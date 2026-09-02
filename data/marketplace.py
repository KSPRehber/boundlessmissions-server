"""
data/marketplace.py – Firestore + Firebase Storage helpers for the craft marketplace.

A listing is a craft (.craft blueprint) a player put up for sale. Listings are
non-exclusive: buying one transfers KCoins to the seller and delivers the buyer a
copy of the blueprint, but the listing stays active so anyone else can buy it too.
Crafts are listed from the KSP mod's Market panel and bought on the website; Discord
is out of it entirely (see cogs/marketplace.py).

Firestore structure (GLOBAL — the marketplace spans every server):
    marketplace/{listing_id} → { ...listing fields..., guild_id (origin), mirrors }
    marketplace_votes/{user_id} → { votes: { listing_id: 1 | -1 } }
    marketplace_reports/{listing_id}_{reporter_id} → { ...one report... }
"""
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from firebase_admin import firestore

from data.store import (_db, _storage_bucket, safe_filename, safe_content_type,
                        upload_private)

log = logging.getLogger(__name__)

# Status constants
ACTIVE = "active"
DELISTED = "delisted"
# The document exists but its craft is not in Storage yet. A listing is created
# before its craft is uploaded (the upload needs the id), and it used to be
# created ACTIVE — so a failed upload left an empty craft on the grid that a
# buyer could pay for. PENDING keeps it off `list_active` until the upload has
# landed; a failed upload deletes it (see marketplace_list_craft).
PENDING = "pending"

# Vote values. A vote is a tri-state, not a toggle: the client always sends the
# state it wants (NONE clears), so a double-click can't flip an unrelated later
# state back on.
VOTE_UP = 1
VOTE_DOWN = -1
VOTE_NONE = 0

ListingData = dict[str, Any]


def _col():
    """The single global marketplace collection (listings are visible/buyable in
    every server). guild_id is recorded on the doc as the origin only."""
    return _db.collection("marketplace")


def create_listing(
    guild_id: int, seller_id: int, seller_name: str,
    craft_name: str, craft_type: str, part_count: int,
    mass: float, cost: float, price: int,
    craft_url: str, craft_filename: str,
    blueprint_url: str = "",
    thumbnail_url: str = "",
    mods: list[str] | None = None,
    parts: list[str] | None = None,
    life_support: str = "none",
    ls_endurance_days: float = 0.0,
    ls_crew_capacity: int = 0,
    custom_textures: bool = False,
    craft_hashes: list[str] | None = None,
    status: str = ACTIVE,
) -> ListingData:
    lid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: ListingData = {
        "listing_id": lid,
        "guild_id": str(guild_id),
        "seller_id": str(seller_id),
        "seller_name": seller_name,
        "craft_name": craft_name,
        "craft_type": craft_type,
        "part_count": part_count,
        "mass": mass,
        "cost": cost,
        "price": price,
        "craft_url": craft_url,
        "craft_filename": craft_filename,
        "blueprint_url": blueprint_url,
        # Square NW-view render shown on the website's listing cards (the full
        # multi-view blueprint_url is reserved for the detail view). Empty for
        # listings made before the thumbnail existed — the site falls back to the
        # blueprint there.
        "thumbnail_url": thumbnail_url,
        # Distinct GameData mod folders the craft uses, sent by the KSP client at
        # list-time. Empty for stock-only crafts or listings made before mod tagging
        # existed. Powers the website's "filter by mod" facet.
        "mods": mods or [],
        # Exact part names the craft uses, sent by the KSP client at list-time. Powers
        # the pre-purchase compatibility check against a buyer's uploaded part catalog,
        # which "mods" cannot do: having the mod is no guarantee of having the part.
        # Empty for listings made before part tagging existed — the check reports
        # "unknown" for those rather than guessing.
        "parts": parts or [],
        # Life-support flag sent by the KSP client: which LS mod the craft is provisioned
        # for ("none"/"usi"/"tac"/"snacks"/"kerbalism"), how many in-game days it lasts per
        # kerbal, and its crew capacity — together these give the min/max endurance range
        # ("X days for 1 kerbal … Y days for a full crew of N") shown on listings.
        "life_support": life_support or "none",
        "ls_endurance_days": float(ls_endurance_days or 0.0),
        "ls_crew_capacity": int(ls_crew_capacity or 0),
        # Whether the craft carries a custom paint job, sent by the KSP client at
        # list-time (TextureTransfer.CraftHasCustomTextures for Textures Unlimited,
        # ReforgedTransfer.CraftHasPaint for Reforged Materials Redux) — the website's
        # "Modded Textures Available" tag. A flag of its own rather than a read of
        # `mods`: a texture set the seller can't resolve either contributes no folder
        # while the paint job is still on the craft, and Reforged's own folder is only
        # added once the client has judged the craft actually painted. False for listings
        # made before the flag existed; those fall back to the mod row
        # (see _has_custom_textures).
        "custom_textures": bool(custom_textures),
        # Fingerprints of the craft file, as "exact:<sha>" / "design:<sha>" /
        # "parts:<sha>" (see data/craft_bans.py). Stored so a craft ban can find
        # every listing that IS the banned craft with one array-contains query —
        # the alternative, hashing at ban time, means downloading every craft in
        # the market. Empty for listings made before fingerprinting existed;
        # those are matched only when a moderator bans from the listing itself,
        # which hashes that one file on demand.
        "craft_hashes": list(craft_hashes or []),
        "status": status,
        "created_at": now,
        # Vote tallies. These are *derived* counters kept in step with the per-user
        # vote records in `marketplace_votes` (see set_vote) — cheap to read on a
        # 25-card grid, which counting the real votes per listing would not be.
        "likes": 0,
        "dislikes": 0,
        # How many distinct users have reported this listing (one report per user
        # per listing — see record_report). Never shown publicly; it exists so the
        # owner console can sort by "most complained about".
        "report_count": 0,
        # Set when the community rating buried this listing (see claim_auto_delist).
        # Kept apart from `status` because "the seller took it down" and "the score
        # took it down" are the same status and two different things to say.
        "auto_delisted": False,
        # Dead field, kept so a listing written now has the same shape as one written
        # before Discord stopped mirroring listings: [{guild_id, channel_id, message_id}].
        # Nothing writes it and nothing reads it any more.
        "mirrors": [],
        "buyers": [],
        "sales_count": 0,
    }
    _col().document(lid).set(doc)
    invalidate_active_cache()
    log.info("Listing %s created: %s selling %s for %d", lid, seller_name, craft_name, price)
    return doc


def get_listing(guild_id: int, listing_id: str) -> ListingData | None:
    snap = _col().document(listing_id).get()
    return snap.to_dict() if snap.exists else None


def update_listing(guild_id: int, listing_id: str, **fields) -> None:
    _col().document(listing_id).update(fields)
    invalidate_active_cache()


# The active market, memoised. This query streams every ACTIVE document — one
# metered Firestore read per listing — and it backs the site's public catalog,
# which is unauthenticated and CDN-cached on a URL the caller controls. So a
# visitor able to vary the query string could turn one browse into a full scan,
# repeatedly; the CDN was the only thing between that and the bill `cost_guard`
# exists to defend. A short TTL here is the backstop that does not depend on the
# cache being hit. Kept deliberately shorter than the CDN's own s-maxage so the
# grid is never staler than the page already is.
_ACTIVE_CACHE: dict[str, object] = {"at": 0.0, "rows": [], "gen": 0}
_ACTIVE_TTL = 30.0

# The console's view of the same collection, memoised the same way and for a
# sharper version of the same reason. `list_all` streams EVERY listing whatever
# its status — one metered Firestore read per document — and it was the one
# marketplace read with no cache at all, behind a 240-per-minute bucket, reachable
# by a mapped guild-admin whose whole tier is deliberately narrower than the
# owner's. The guild scoping is applied to the rows *after* the read, so it
# reduces what is shown and not what is billed: 240 × N reads a minute from a role
# that is not supposed to be able to spend anything. Same TTL and the same
# invalidation, so a moderator's own edit still shows up immediately.
_ALL_CACHE: dict[str, object] = {"at": 0.0, "rows": [], "gen": 0}


def list_active(guild_id: int) -> list[ListingData]:
    """All active listings, globally (guild_id ignored — one shared market).

    Answers from a 30-second in-process cache; `invalidate_active_cache()` clears it
    on every write so a new listing, a purchase or a delist shows up at once rather
    than after the TTL.
    """
    now = time.time()
    # Snapshot both fields once. Reading `["rows"]` twice let an invalidate landing
    # between the check and the return hand back an empty list — an empty
    # marketplace page — since this runs in a threadpool while writers invalidate
    # from other threads.
    rows, at = _ACTIVE_CACHE["rows"], float(_ACTIVE_CACHE["at"])
    if rows and now - at < _ACTIVE_TTL:
        return list(rows)                           # a copy: callers sort in place
    # A generation counter, taken before the stream and re-checked after it: a
    # writer that invalidates while this query is in flight bumps it, and the fill
    # is then discarded rather than writing rows that predate the write back into
    # the cache with a fresh timestamp (which would resurrect a delisted listing
    # for a full TTL, and 404 anyone who tried to buy it).
    gen = _ACTIVE_CACHE["gen"]
    rows = [
        doc.to_dict()
        for doc in _col().where("status", "==", ACTIVE).stream()
    ]
    if _ACTIVE_CACHE["gen"] == gen:
        _ACTIVE_CACHE["rows"] = rows
        _ACTIVE_CACHE["at"] = time.time()
    return list(rows)


def invalidate_active_cache() -> None:
    """Drop the memoised listing sets — the public active grid and the console's
    all-statuses view. Every writer of a listing's stored fields calls this —
    including `_set_vote_locked`, whose counters feed the score and both rating
    sorts. One function for both caches so a new writer cannot remember one and
    forget the other; a delist has to leave the grid AND appear as delisted in the
    console, and those are the same event."""
    for cache in (_ACTIVE_CACHE, _ALL_CACHE):
        cache["rows"] = []
        cache["at"] = 0.0
        cache["gen"] = int(cache["gen"]) + 1


def list_by_seller(seller_id: int) -> list[ListingData]:
    """Every listing a user created (active AND delisted) — the website's
    "My Uploads" view, where the seller can still see and delist their crafts."""
    return [
        doc.to_dict()
        for doc in _col().where("seller_id", "==", str(seller_id)).stream()
    ]


def list_by_buyer(buyer_id: int) -> list[ListingData]:
    """Every listing a user has bought (so the website can offer a free
    re-download under "My Purchases"). Firestore has no "array contains" index
    requirement issue here — it's a single array-contains on `buyers`."""
    return [
        doc.to_dict()
        for doc in _col().where("buyers", "array_contains", str(buyer_id)).stream()
    ]


def list_by_hash(entry: str) -> list[ListingData]:
    """Every listing whose craft carries this "kind:hash" fingerprint, any status.

    One array-contains query rather than a scan: issuing a ban has to sweep the
    market, and a market that has to be read whole to be swept is one that stops
    being swept once it is big enough to matter."""
    return [
        doc.to_dict()
        for doc in _col().where("craft_hashes", "array_contains", entry).stream()
    ]


def list_all() -> list[ListingData]:
    """Every listing regardless of status or seller — the admin console's view
    (it must see delisted crafts to be able to moderate them).

    Answers from the same 30-second cache `list_active` uses, cleared by the same
    `invalidate_active_cache()` writers, and with the same generation counter so a
    write landing mid-stream discards the fill instead of caching rows that
    predate it.
    """
    now = time.time()
    rows, at = _ALL_CACHE["rows"], float(_ALL_CACHE["at"])
    if rows and now - at < _ACTIVE_TTL:
        return list(rows)                           # a copy: callers filter/sort it
    gen = _ALL_CACHE["gen"]
    rows = [doc.to_dict() for doc in _col().stream()]
    if _ALL_CACHE["gen"] == gen:
        _ALL_CACHE["rows"] = rows
        _ALL_CACHE["at"] = time.time()
    return list(rows)


def delete_listing(listing_id: str) -> None:
    """Permanently remove a listing: its Storage files (craft + blueprint, the whole
    marketplace/{id}/ prefix) and the Firestore document. Best-effort on Storage so a
    missing bucket/file never blocks deleting the record. Irreversible."""
    if _storage_bucket is not None:
        try:
            for blob in _storage_bucket.list_blobs(prefix=f"marketplace/{listing_id}/"):
                try:
                    blob.delete()
                except Exception as exc:
                    log.warning("Could not delete blob %s: %s", blob.name, exc)
        except Exception as exc:
            log.warning("Could not list Storage blobs for listing %s: %s", listing_id, exc)
    _col().document(listing_id).delete()
    log.info("Listing %s permanently deleted", listing_id)
    invalidate_active_cache()


def try_claim_purchase(guild_id: int, listing_id: str, buyer_id: int) -> bool | None:
    """Atomically record `buyer_id` as a buyer of a listing (append + bump sales).

    Returns True when this call actually added the buyer (a genuinely new purchase),
    False when the buyer was already recorded (a duplicate/concurrent buy), or None
    when the listing is gone.

    The claim runs in a Firestore transaction, which is what makes it a safe charge
    gate: of two concurrent buys of the same craft by the same user, exactly one gets
    True. The caller keeps the buyer's debit only on that True and refunds the loser,
    so a double-submit can never charge twice for one craft (see web_marketplace_buy).
    The transaction.get / transaction.update calls pass through the cost-guard proxy
    unchanged — guarded refs handed to transaction methods are unwrapped there.
    """
    ref = _col().document(listing_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _claim(txn) -> bool | None:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        buyers = list(data.get("buyers", []) or [])
        if str(buyer_id) in buyers:
            return False
        buyers.append(str(buyer_id))
        txn.update(ref, {
            "buyers": buyers,
            "sales_count": int(data.get("sales_count", 0) or 0) + 1,
        })
        return True

    result = _claim(transaction)
    if result:
        # sales_count and buyers are shown on the card, so the memoised set is stale.
        invalidate_active_cache()
    return result


# ── Votes ────────────────────────────────────────────────────────────────────
#
# Who voted what lives in ONE document per user (`marketplace_votes/{user_id}`),
# not one per (user, listing). The website needs "which of these 25 crafts have I
# voted on?" on every grid load, and a per-pair layout answers that with either 25
# reads or a collection-group query behind a composite index; a per-user map
# answers it with a single read. The tallies on the listing are the mirror image
# of the same trade-off: a count that is read on every card is stored, not summed.
#
# The two are kept in step by set_vote, which is the only writer of either.

def _votes_doc(user_id: int | str):
    return _db.collection("marketplace_votes").document(str(user_id))


# set_vote is read-then-increment: the Increment is atomic, the read that decides
# the delta is not. The endpoint runs it in a thread pool, so N identical votes
# from one account arriving together each read "no previous vote" and each add
# one — enough, at 25 in a burst, to push any listing through the auto-delist
# floor. One account's votes are therefore serialised in-process: a fixed stripe
# of locks keyed by the voter, so the dict is bounded and the same user always
# lands on the same lock. This holds while the API is one process (the same
# assumption every contract_actions transition rests on); a second worker would
# need the read and the write inside one Firestore transaction instead.
_VOTE_LOCK_STRIPES = 64
_vote_locks = [threading.Lock() for _ in range(_VOTE_LOCK_STRIPES)]


def _vote_lock(user_id: int | str) -> threading.Lock:
    h = int(hashlib.sha1(str(user_id).encode()).hexdigest()[:8], 16)
    return _vote_locks[h % _VOTE_LOCK_STRIPES]


def get_user_votes(user_id: int | str) -> dict[str, int]:
    """Every vote this user has cast: {listing_id: 1 | -1}. Empty for a user who
    has never voted (no document), which is the common case."""
    snap = _votes_doc(user_id).get()
    if not snap.exists:
        return {}
    raw = (snap.to_dict() or {}).get("votes") or {}
    return {k: int(v) for k, v in raw.items() if int(v) in (VOTE_UP, VOTE_DOWN)}


def set_vote(listing_id: str, user_id: int | str, vote: int) -> tuple[int, int] | None:
    """Record `vote` (VOTE_UP / VOTE_DOWN / VOTE_NONE) by `user_id` on a listing.

    Returns the listing's (likes, dislikes) after the change, or None if the
    listing is gone. Idempotent: re-sending the same vote changes nothing.

    The tally is moved with a Firestore Increment (atomic — two users voting at
    once can't clobber each other) and only then is the user's own record written;
    if that second write fails the increment is undone, so the counter never keeps
    a vote nobody is recorded as having cast.
    """
    vote = VOTE_UP if vote > 0 else (VOTE_DOWN if vote < 0 else VOTE_NONE)
    with _vote_lock(user_id):
        return _set_vote_locked(listing_id, user_id, vote)


def _set_vote_locked(listing_id: str, user_id: int | str, vote: int) -> tuple[int, int] | None:
    """The body of set_vote; the caller holds this user's vote lock, so `old` is
    what the user's record really says and not what it said before a sibling
    request wrote it."""
    ref = _col().document(listing_id)
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}

    old = int(get_user_votes(user_id).get(listing_id, VOTE_NONE))
    likes = max(0, int(data.get("likes", 0) or 0))
    dislikes = max(0, int(data.get("dislikes", 0) or 0))
    if old == vote:
        return likes, dislikes

    d_like = (1 if vote == VOTE_UP else 0) - (1 if old == VOTE_UP else 0)
    d_dislike = (1 if vote == VOTE_DOWN else 0) - (1 if old == VOTE_DOWN else 0)

    ref.update({"likes": firestore.Increment(d_like),
                "dislikes": firestore.Increment(d_dislike)})
    try:
        # DELETE_FIELD rather than a stored 0: the map is the list of votes actually
        # cast, so a cleared vote must leave nothing behind. set(merge=True) is used
        # for both cases because update() would fail for a user voting for the first
        # time, whose document does not exist yet.
        _votes_doc(user_id).set(
            {"votes": {listing_id: firestore.DELETE_FIELD if vote == VOTE_NONE else vote}},
            merge=True,
        )
    except Exception:
        ref.update({"likes": firestore.Increment(-d_like),
                    "dislikes": firestore.Increment(-d_dislike)})
        invalidate_active_cache()   # the compensating write moved the counters too
        raise

    # A vote changes `likes`/`dislikes`, which every card's score and both the
    # "highest rated" and "recommended" sorts are computed from — so the memoised
    # set is stale the moment this returns. Missing this made a vote invisible to
    # everyone but the voter (whose own view is masked by the optimistic local
    # delta in use-listing-votes.ts) for up to a TTL.
    invalidate_active_cache()
    return max(0, likes + d_like), max(0, dislikes + d_dislike)


def net_score(listing: ListingData) -> int:
    """A listing's rating: likes minus dislikes, one signed number.

    This is what the website shows and what the "highest rated" sort ranks by; the
    two tallies behind it are storage detail. Derived rather than stored because a
    third counter is a third thing that can drift out of step with the votes."""
    return (max(0, int(listing.get("likes", 0) or 0))
            - max(0, int(listing.get("dislikes", 0) or 0)))


def claim_auto_delist(listing_id: str, score: int) -> bool:
    """Take a listing off the grid because its score reached the floor.

    Returns True only for the call that actually did it — a still-active listing
    flipped to delisted here and now. Two downvotes landing together both see an
    active listing, so the flip runs in a transaction and the loser gets False:
    the removal is idempotent either way, but the seller must not be told twice
    that their craft was removed once.

    The marker is written alongside the status so the seller's My Uploads view can
    say *why* the craft is down — a listing that delisted itself while they were
    offline is otherwise indistinguishable from one they delisted themselves.
    """
    ref = _col().document(listing_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _claim(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or (snap.to_dict() or {}).get("status") != ACTIVE:
            return False
        txn.update(ref, {
            "status": DELISTED,
            "auto_delisted": True,
            "auto_delisted_at": datetime.utcnow().isoformat(),
            "auto_delisted_score": int(score),
        })
        return True

    claimed = bool(_claim(transaction))
    if claimed:
        invalidate_active_cache()   # it just left the grid
        log.info("Listing %s auto-delisted at score %d", listing_id, score)
    return claimed


def clear_auto_delisted(listing_id: str) -> None:
    """Drop the auto-delist marker (a moderator put the listing back up).

    The marker is only ever a note about the past, so it must not outlive the state
    it describes — left behind it would keep telling a seller their live listing was
    removed. Whether the craft can be buried *again* is decided from the live score,
    not from this flag."""
    _col().document(listing_id).update({
        "auto_delisted": False,
        "auto_delisted_at": firestore.DELETE_FIELD,
        "auto_delisted_score": firestore.DELETE_FIELD,
    })


# ── Reports ──────────────────────────────────────────────────────────────────

def _report_id(listing_id: str, reporter_id: int | str) -> str:
    return f"{listing_id}_{reporter_id}"


def get_report(listing_id: str, reporter_id: int | str) -> dict[str, Any] | None:
    """This user's existing report against this listing, if any. The document id
    is the (listing, reporter) pair, so "have I already reported this?" is a
    single keyed read — no composite index, and no way to file the same complaint
    twice to make it look louder."""
    snap = _db.collection("marketplace_reports").document(
        _report_id(listing_id, reporter_id)).get()
    return snap.to_dict() if snap.exists else None


def record_report(listing: ListingData, reporter_id: int | str, reporter_name: str,
                  reason: str, guild_id: int | str = "",
                  ticket_channel_id: int | str = "") -> None:
    """Store a report and bump the listing's report_count.

    The Discord ticket is where a report is actually *handled*; this record exists
    so the count survives the ticket being closed, and so a second report from the
    same user overwrites rather than accumulates."""
    listing_id = listing["listing_id"]
    first_time = get_report(listing_id, reporter_id) is None
    _db.collection("marketplace_reports").document(_report_id(listing_id, reporter_id)).set({
        "listing_id": listing_id,
        "craft_name": listing.get("craft_name", ""),
        "seller_id": str(listing.get("seller_id", "")),
        "seller_name": listing.get("seller_name", ""),
        "reporter_id": str(reporter_id),
        "reporter_name": reporter_name,
        "reason": reason,
        "guild_id": str(guild_id),
        "ticket_channel_id": str(ticket_channel_id),
        "created_at": datetime.utcnow().isoformat(),
    })
    if first_time:
        try:
            _col().document(listing_id).update({"report_count": firestore.Increment(1)})
        except Exception as exc:  # a missing listing must not lose the report
            log.warning("Could not bump report_count for listing %s: %s", listing_id, exc)
    log.info("Listing %s reported by %s (%s)", listing_id, reporter_name, reporter_id)


async def upload_craft(listing_id: str, filename: str, data: bytes) -> str:
    """Upload a raw (decompressed) .craft file to Storage as a PRIVATE object and
    return its bucket path (not a public URL).

    A listing's craft is behind the paywall: the download URL is minted (signed) only
    for the buyer / owner surfaces (buy result, My Purchases, My Uploads) and is
    withheld from the public grid. The browser download still forces a save because
    the website's /api/marketplace/download proxy streams it with
    Content-Disposition: attachment — so no per-object content_disposition is needed."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    name = safe_filename(filename, 'craft.craft')
    path = f"marketplace/{listing_id}/{name}"
    return upload_private(path, data, content_type="text/plain")


async def upload_blueprint(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload a rendered blueprint image for a listing. Returns public URL.

    The content type goes through `safe_content_type` for the reason it exists
    (see data/store.py): this blob is made public and the website links it, so a
    client-declared `text/html` or `image/svg+xml` would have the project's own
    bucket serving attacker-authored active content from a URL the site publishes.
    These two uploaders were the only public ones that skipped it.
    """
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/blueprint.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type))
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def upload_thumbnail(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload the square NW-view thumbnail for a listing (website card). Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/thumbnail.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type))
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url
