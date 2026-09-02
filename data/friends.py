"""
data/friends.py – who a player has agreed to exchange craft with.

The quicksend picker used to be `/api/v1/corps/list`: every corp in the caller's
guild, plus every guild-less website account. That is a *roster*, not a friend
list, and it made a hand-over — the send that removes a live vessel from the
sender's save and drops it into somebody else's — something any stranger in a
large Discord could be on the receiving end of. `Favorites.cs` was the only thing
resembling friendship and it is neither shared nor mutual: a local star file, on
one machine, keyed on Discord snowflakes, that a website account could never
appear in.

So friendship here is:

  * **mutual and explicit** — a request, then an accept. Both halves are written,
    so "are we friends" is one read of the asker's own document and never a scan.
  * **keyed on account ids**, exactly as `data/accounts.py` defines them. A
    Discord-origin id is the snowflake, a website one is `a_<firebase uid>`, and
    neither side of a friendship cares which it is holding. That is the whole
    reason this module exists rather than a snowflake-keyed one: a Boundless
    account with no Discord is a first-class friend, findable by the username it
    already had to claim before it could publish a name to anyone.
  * **guild-independent**. A friendship is between two people, not between two
    people *in a server* — the same reasoning that moved the wallet to a
    top-level `users/{user_id}`. Two players who met in different Discords and
    linked through different guilds are still each other's friends.

Document shape (`friends/{account_id}` — one per player, not a doc per pair):

    {
      "friends":  { "<other_id>": {"since": <epoch>} },
      "incoming": { "<other_id>": {"at": <epoch>} },   # they asked me
      "outgoing": { "<other_id>": {"at": <epoch>} },   # I asked them
      "updated":  <epoch>,
    }

One document read answers every question a client asks — the list, the pending
requests in both directions, and "may I send to this person" — which is the same
trade `marketplace_votes/{user_id}` makes and for the same reason: this project's
`cost_guard` exists because Firestore operations are the bill being defended
against, and a collection-group query per picker open would be one read per
friend instead of one per player.

Every mutation touches *two* documents and so runs in a transaction. Half an
accept is the one state this module must never leave behind: a friendship that
exists on one side only would let one player send to someone who cannot send
back and cannot see why, and no repair pass would know which of the two records
was the true one.

Reads fail **closed** here, unlike suspensions and craft bans. Those two gate
*abuse*, where an outage that refused every upload would be far worse than the
thing being prevented; this gates a hand-over of somebody's ship into a stranger's
save. A Firestore blip is a few minutes of "couldn't check, try again", which is
recoverable. The alternative is not.
"""

import logging
import time

from firebase_admin import firestore

from data.store import _db

log = logging.getLogger(__name__)

# Caps. Friendship is a list of people you actually play with, and every one of
# these numbers exists to bound a *document*: the whole record is read on every
# picker open and rewritten on every change, and Firestore's limit is 1 MiB.
MAX_FRIENDS = 250
MAX_INCOMING = 100
MAX_OUTGOING = 50

_EMPTY = {"friends": {}, "incoming": {}, "outgoing": {}}


class FriendsUnavailable(Exception):
    """The record could not be read. Callers must refuse, never assume."""


def _col():
    return _db.collection("friends")


def _now() -> float:
    return time.time()


def _norm(record: dict | None) -> dict:
    """A stored record with its three maps guaranteed present.

    Written defensively because the document is created lazily: the first friend
    request either player ever sends is also the first time either document
    exists, and every read below has to cope with a half-shaped or absent one.
    """
    d = dict(record or {})
    for key in ("friends", "incoming", "outgoing"):
        val = d.get(key)
        d[key] = dict(val) if isinstance(val, dict) else {}
    return d


# ── Reads ────────────────────────────────────────────────────────────────────

def get_record(account_id) -> dict:
    """One player's friends and pending requests. Raises on a failed read."""
    aid = str(account_id)
    try:
        snap = _col().document(aid).get()
    except Exception as exc:
        log.warning("Friend record read failed for %s: %s", aid, exc)
        raise FriendsUnavailable(str(exc)) from exc
    return _norm(snap.to_dict() if snap.exists else None)


def friend_ids(account_id) -> list[str]:
    """Just the accepted friends, newest friendship first."""
    rec = get_record(account_id)
    pairs = sorted(rec["friends"].items(),
                   key=lambda kv: -float((kv[1] or {}).get("since", 0) or 0))
    return [k for k, _v in pairs]


def are_friends(a, b) -> bool:
    """Whether these two have accepted each other.

    Only `a`'s document is read: both halves are written together in one
    transaction, so either answers the question and reading both would double the
    cost of every send for no extra certainty.
    """
    return str(b) in get_record(a)["friends"]


def relationship(account_id, other) -> str:
    """"friends" | "outgoing" | "incoming" | "none" — what the client draws."""
    rec = get_record(account_id)
    oid = str(other)
    if oid in rec["friends"]:
        return "friends"
    if oid in rec["outgoing"]:
        return "outgoing"
    if oid in rec["incoming"]:
        return "incoming"
    return "none"


# ── Writes ───────────────────────────────────────────────────────────────────
#
# Each of these reads both documents inside the transaction and writes both.
#
# The write is a **whole-document** `set` with no merge, and that is not an
# oversight: `merge=True` deep-merges nested maps, so a key removed from
# `friends` in memory would simply survive the write and the unfriend would do
# nothing. These four fields are the entire document and every one of them was
# just read inside this transaction, so replacing it wholesale is both correct
# and the only shape that can express a deletion. It also creates the document
# lazily, which is what the request path needs — the first request either player
# sends is the first time either record exists.

def _write_pair(txn, a_ref, a_rec, b_ref, b_rec) -> None:
    now = _now()
    txn.set(a_ref, _doc(a_rec, now))
    txn.set(b_ref, _doc(b_rec, now))


def _doc(rec: dict, now: float) -> dict:
    return {
        "friends": rec["friends"],
        "incoming": rec["incoming"],
        "outgoing": rec["outgoing"],
        "updated": now,
    }


def send_request(from_id, to_id) -> tuple[bool, str, str]:
    """Ask `to_id` to be friends.

    Returns (ok, state, message) where state is one of "requested", "accepted"
    (they had already asked us, so the request completes the handshake rather
    than queueing behind it — asking someone who asked you first is an accept in
    every messaging app there is, and queueing it would leave two requests that
    each look unanswered) or "already" / "" for the refusals.
    """
    a, b = str(from_id), str(to_id)
    if a == b:
        return False, "", "You can't add yourself."

    a_ref, b_ref = _col().document(a), _col().document(b)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> tuple[bool, str, str]:
        a_rec = _norm(_get(txn, a_ref))
        b_rec = _norm(_get(txn, b_ref))

        if b in a_rec["friends"]:
            return False, "already", "You're already friends."
        if b in a_rec["outgoing"]:
            return False, "already", "You've already sent them a request."

        if b in a_rec["incoming"]:
            # They asked first. Complete it.
            _link(a_rec, b_rec, a, b)
            _write_pair(txn, a_ref, a_rec, b_ref, b_rec)
            return True, "accepted", "You're now friends."

        if len(a_rec["outgoing"]) >= MAX_OUTGOING:
            return False, "", (
                f"You have {MAX_OUTGOING} friend requests waiting for an answer. "
                "Cancel one before sending another.")
        if len(a_rec["friends"]) >= MAX_FRIENDS:
            return False, "", f"Your friend list is full ({MAX_FRIENDS})."
        if len(b_rec["friends"]) >= MAX_FRIENDS or len(b_rec["incoming"]) >= MAX_INCOMING:
            # Deliberately vague: the exact reason is a fact about somebody
            # else's account, and "they can't take one right now" is all the
            # sender can act on either way.
            return False, "", "That player can't take a friend request right now."

        now = _now()
        a_rec["outgoing"][b] = {"at": now}
        b_rec["incoming"][a] = {"at": now}
        _write_pair(txn, a_ref, a_rec, b_ref, b_rec)
        return True, "requested", "Friend request sent."

    try:
        return _run(transaction)
    except FriendsUnavailable:
        raise
    except Exception as exc:
        log.warning("Friend request %s -> %s failed: %s", a, b, exc)
        raise FriendsUnavailable(str(exc)) from exc


def accept_request(account_id, other) -> tuple[bool, str]:
    """Accept a request `other` sent us."""
    a, b = str(account_id), str(other)
    a_ref, b_ref = _col().document(a), _col().document(b)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> tuple[bool, str]:
        a_rec = _norm(_get(txn, a_ref))
        b_rec = _norm(_get(txn, b_ref))
        if b in a_rec["friends"]:
            return True, "You're already friends."
        if b not in a_rec["incoming"]:
            return False, "There's no request from that player."
        if len(a_rec["friends"]) >= MAX_FRIENDS:
            return False, f"Your friend list is full ({MAX_FRIENDS})."
        if len(b_rec["friends"]) >= MAX_FRIENDS:
            return False, "That player's friend list is full."
        _link(a_rec, b_rec, a, b)
        _write_pair(txn, a_ref, a_rec, b_ref, b_rec)
        return True, "You're now friends."

    try:
        return _run(transaction)
    except Exception as exc:
        log.warning("Friend accept %s <- %s failed: %s", a, b, exc)
        raise FriendsUnavailable(str(exc)) from exc


def cancel_request(account_id, other) -> tuple[bool, str]:
    """Withdraw a request we sent, or turn down one we received.

    One function for both directions on purpose: to the storage they are the same
    edit — drop the pair from `outgoing` on one side and `incoming` on the other —
    and two functions would be two chances to drop only one half. The wording the
    player sees is chosen by the caller, which is the only part that differs.
    """
    a, b = str(account_id), str(other)
    a_ref, b_ref = _col().document(a), _col().document(b)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> tuple[bool, str]:
        a_rec = _norm(_get(txn, a_ref))
        b_rec = _norm(_get(txn, b_ref))
        touched = False
        for rec, key in ((a_rec, "incoming"), (a_rec, "outgoing")):
            if b in rec[key]:
                rec[key].pop(b, None)
                touched = True
        for rec, key in ((b_rec, "incoming"), (b_rec, "outgoing")):
            if a in rec[key]:
                rec[key].pop(a, None)
                touched = True
        if not touched:
            return False, "There's no pending request with that player."
        _write_pair(txn, a_ref, a_rec, b_ref, b_rec)
        return True, "Request removed."

    try:
        return _run(transaction)
    except Exception as exc:
        log.warning("Friend cancel %s / %s failed: %s", a, b, exc)
        raise FriendsUnavailable(str(exc)) from exc


def decline_all(account_id) -> int:
    """Turn down every pending incoming request at once. Returns how many went.

    `MAX_INCOMING` is a bound on the *document*, but it is also a weapon: a
    hundred free accounts, one request each, fill a victim's inbox and every
    honest request after that is refused — and since `/api/v1/craft/send` gates on
    `are_friends`, nobody new can be quicksent a craft until they clear it.
    Clearing it used to be a hundred separate declines, each its own two-document
    transaction, against an attacker who can refill faster than that. This is that
    clean-up as one action.

    It is a bulk `cancel_request`, not a new mechanism: the same edit (drop the
    pair from my `incoming` and from each peer's `outgoing`), in the same
    whole-document `set` with no merge, in ONE transaction — because half of this
    is the state the module exists to make unreachable, and a hundred separate
    transactions is a hundred chances to stop half way. Bounded by MAX_INCOMING,
    so this is at most 101 reads and 101 writes, well inside a transaction's 500.

    Deliberately no notifications: a decline is already silent (`cancel_request`
    sends none), and a hundred of them would be a hundred pushes announcing that
    somebody's flood was cleaned up.
    """
    a = str(account_id)
    a_ref = _col().document(a)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> int:
        a_rec = _norm(_get(txn, a_ref))
        peers = list(a_rec["incoming"].keys())
        if not peers:
            return 0
        # Every read before any write: a Firestore transaction refuses a read
        # that follows a write, so the peers are gathered first and edited after.
        peer_refs = {p: _col().document(p) for p in peers}
        peer_recs = {p: _norm(_get(txn, ref)) for p, ref in peer_refs.items()}

        now = _now()
        a_rec["incoming"] = {}
        txn.set(a_ref, _doc(a_rec, now))
        for p, rec in peer_recs.items():
            # Only their side of THIS pair — a peer's own friends and their other
            # pending requests are none of this operation's business.
            rec["outgoing"].pop(a, None)
            txn.set(peer_refs[p], _doc(rec, now))
        return len(peers)

    try:
        return _run(transaction)
    except Exception as exc:
        log.warning("Friend decline-all for %s failed: %s", a, exc)
        raise FriendsUnavailable(str(exc)) from exc


def remove_friend(account_id, other) -> tuple[bool, str]:
    """Unfriend, both ways.

    Removing is deliberately one-sided in *authority* and two-sided in *effect*:
    either party can do it, and it ends the friendship rather than muting it,
    because the only thing friendship grants is the right to hand this person a
    craft — and "they can still send to me but I can't see them" is not a state
    worth being able to reach.
    """
    a, b = str(account_id), str(other)
    a_ref, b_ref = _col().document(a), _col().document(b)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> tuple[bool, str]:
        a_rec = _norm(_get(txn, a_ref))
        b_rec = _norm(_get(txn, b_ref))
        if b not in a_rec["friends"] and a not in b_rec["friends"]:
            return False, "You're not friends with that player."
        a_rec["friends"].pop(b, None)
        b_rec["friends"].pop(a, None)
        # Any stale pending entry goes with it, or an unfriend would leave a
        # request that can never be answered because the button is gone.
        a_rec["incoming"].pop(b, None); a_rec["outgoing"].pop(b, None)
        b_rec["incoming"].pop(a, None); b_rec["outgoing"].pop(a, None)
        _write_pair(txn, a_ref, a_rec, b_ref, b_rec)
        return True, "Removed."

    try:
        return _run(transaction)
    except Exception as exc:
        log.warning("Friend remove %s / %s failed: %s", a, b, exc)
        raise FriendsUnavailable(str(exc)) from exc


def forget_account(account_id) -> None:
    """Erase this account from the friend graph — the data-purge path.

    Deleting only `friends/{id}` would leave the account id in every friend's
    document, where it would keep drawing as a nameless row nobody can remove.
    Best-effort and not transactional: it runs after the account is already gone,
    so there is no consistent state left to protect, only litter to sweep.
    """
    aid = str(account_id)
    try:
        rec = get_record(aid)
    except FriendsUnavailable:
        return
    others = set(rec["friends"]) | set(rec["incoming"]) | set(rec["outgoing"])
    for other in others:
        try:
            ref = _col().document(other)
            snap = ref.get()
            if not snap.exists:
                continue
            peer = _norm(snap.to_dict())
            for key in ("friends", "incoming", "outgoing"):
                peer[key].pop(aid, None)
            ref.set(_doc(peer, _now()))
        except Exception as exc:                   # pragma: no cover - defensive
            log.warning("Friend purge: could not clean %s: %s", other, exc)
    try:
        _col().document(aid).delete()
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Friend purge: could not delete %s: %s", aid, exc)


# ── internals ────────────────────────────────────────────────────────────────

def _get(txn, ref) -> dict | None:
    snap = ref.get(transaction=txn)
    return snap.to_dict() if snap.exists else None


def _link(a_rec: dict, b_rec: dict, a: str, b: str) -> None:
    """Make the two records friends and clear every pending trace of the pair."""
    now = _now()
    a_rec["friends"][b] = {"since": now}
    b_rec["friends"][a] = {"since": now}
    a_rec["incoming"].pop(b, None); a_rec["outgoing"].pop(b, None)
    b_rec["incoming"].pop(a, None); b_rec["outgoing"].pop(a, None)
