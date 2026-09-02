"""
data/craft_bans.py – blocking a *craft* rather than a person.

The moderation surface already answers "this listing is bad" (delist/delete) and
"this player is bad" (data/suspensions.py). Neither answers the case in between:
one craft file that must not circulate — someone else's design uploaded as their
own, a payload that crashes a recipient's game on load, a joke ship named
something unrepeatable. Delisting removes one *listing*; the file itself is
still on the uploader's disk and comes straight back, under a new name, from a
new account, to a listing in a different guild.

So the ban is keyed on the craft, and the key is a hash of it.

WHAT THIS IS NOT
────────────────
A .craft is a plain-text ConfigNode. Anyone determined to get a banned craft past
this can open it in a text editor and nudge one part, and no hash of any kind
will match. That is inherent, not a gap to be plugged: this is nuisance control —
it stops the file being re-uploaded, which is what actually happens — and it is
never a security boundary. Nothing downstream may assume a craft that got past
here is safe.

THE THREE FINGERPRINTS
──────────────────────
A single sha256 over the bytes would be both too strict and too weak to be worth
having, so every craft is fingerprinted three ways and a ban names which one it
is enforcing:

  exact   sha256 of the stored bytes. Zero false positives, and it is the one
          that catches the realistic case — the same file, passed around and
          re-uploaded. It is also brittle by construction: the mod's own export
          chain (bake → GKFLAG → GKTSVER → GKTU → GKRF → GKMODS → GKTHUMB, see
          the KSP mod's ScaleBridge/CraftInstaller) appends side-channel blocks
          and a freshly rendered thumbnail, so the *same* craft exported twice
          from two installs is not byte-identical.

  design  sha256 over the geometry: every part's base name and its position,
          rounded to the centimetre, sorted. Everything a re-export or a rename
          touches is gone from the input — ship name, description, flag, the GK
          blocks, module state, part ids — so this survives "open it, rename it,
          save it, re-upload it", which `exact` does not. Two people who
          independently built similar rockets do not collide here: agreeing on
          every part *and* every position to the centimetre means one craft is a
          copy of the other, which is exactly the finding a ban wants.

  parts   sha256 over the sorted part names alone, positions discarded. Catches
          the craft that was banned and then had one part nudged. It is also the
          one that can genuinely over-match — two different ships built from the
          same parts bin hash the same — so it is never applied by default and
          the console warns before issuing one.

All three are computed for every upload; a ban stores one. Which to use is a
moderator's judgement about how hard someone is trying, and it is theirs to make
rather than ours to guess.

WHERE IT IS ENFORCED
────────────────────
Every path that takes a craft from a client and hands it to somebody else:
listing it on the marketplace, quicksending it to a friend, and submitting it
against a contract. Relisting is covered too, from the fingerprint stored on the
listing rather than a re-download.

Two places deliberately do **not** check. Issuing a rescue uploads the issuer's
own broken ship so somebody can come and get it, and the vessel node of a rescue
*submission* is that same wreck coming home — neither is a design being handed
out, and refusing them would strand a player over a craft that is already theirs.
Downloads are not checked either: the ban sweep delists the matching listings, so
a banned craft has already stopped being buyable, and re-checking on the way out
would mean fetching and hashing a file per download to enforce something already
enforced at the door.

Document shape (`craft_bans/{hash}` — the hash *is* the id, so banning the same
craft twice updates one record instead of accumulating duplicates):

    {
        "hash":       "<sha256 hex>",
        "kind":       "exact" | "design" | "parts",
        "reason":     "Reuploaded someone else's craft",  # SHOWN TO THE PLAYER
        "note":       "Ticket #412",                      # internal, never sent
        "label":      "Kerbal X",        # what the craft was called, for the list
        "listing_id": "ab12…",           # where it was banned from, if anywhere
        "by":         "owner#0",
        "created_at": "<iso8601>",
        "active":     true,
        "revoked_at": null, "revoked_by": null,
        "hits":       3, "last_hit_at": "<iso8601>",
    }

Revoking sets `active = False` rather than deleting, for the same reason a lifted
suspension keeps its document: the record of what was done outlives the undoing.

The check runs on every craft upload, so the whole ban list is cached in-process
for `_CACHE_TTL` and answered from a dict. That is affordable precisely because
bans are rare — this is a list of dozens, not of users — and writers update the
cache in place, so a ban issued in the console takes effect on the next upload
rather than up to a TTL later.

A Firestore read failure fails **open** (nothing is banned), matching
data/suspensions.py: an outage that let a banned craft through for a few minutes
is a much smaller harm than one that refused every upload in the game.
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

EXACT = "exact"
DESIGN = "design"
PARTS = "parts"
KINDS = (EXACT, DESIGN, PARTS)

REASON_MAX = 300
NOTE_MAX = 500
LABEL_MAX = 100

# The fingerprint parser builds one object per part and sorts them all, so an
# oversized payload turns into millions of tuples and a multi-second sort in the
# bot's own process. `_safe_gunzip` bounds the decompressed *bytes* (64 MB) but
# not the object graph built from them, so a ~127 KB gzip bomb of minimal PART
# blocks reached this scanner and cost seconds of CPU and ~1 GB of RSS per
# request. A real .craft or vessel node is far under this cap; past it the craft
# still gets its `exact` hash (a cheap sha256 of the bytes) but no design/parts
# hash, and is flagged `suspect` — the same honest 'could not read this' answer
# the scanner already gives, consistent with the module's 'a text edit defeats
# any hash' threat model.
_MAX_FINGERPRINT_BYTES = 4 * 1024 * 1024

# The default refusal, for a ban issued without one. Deliberately says who did it
# and what to do about it: a flat "no" from the server reads as a bug and arrives
# as a bug report.
DEFAULT_REASON = ("This craft has been blocked by the moderators. "
                  "Contact them if you think that's a mistake.")

_CACHE_TTL = 60.0
# hash -> record, for the active bans only. None until first load.
_index: dict[str, dict] | None = None
_index_at = 0.0


def _col():
    return _db.collection("craft_bans")


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
#  Fingerprinting
# ══════════════════════════════════════════════════════════════════════════════

# A part id in a .craft is "<partname>_<instance id>" — the trailing number is
# assigned when the part is placed and says nothing about which part it is.
_PART_ID_SUFFIX = re.compile(r"_\d+$")
_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _parts_of(text: str) -> list[tuple[str, tuple[float, float, float]]]:
    """Every part in a craft or vessel node, as (base part name, position).

    A hand-rolled brace scanner rather than a real ConfigNode parser because only
    two things are wanted out of the file and everything else — the modules, the
    resources, the action groups, the GK side-channel blocks — is noise that must
    not reach the hash. Tracking depth is what makes the MODULE nodes' own
    `name = ` lines invisible: only a PART node's *direct* children are read.

    Handles both dialects in one pass: a .craft names its part `part = mk1pod_42`,
    a saved VESSEL node names it `name = mk1pod`.
    """
    out: list[tuple[str, tuple[float, float, float]]] = []
    depth = 0
    # Depth at which the PART node we are inside begins, or None when outside one.
    part_depth: int | None = None
    # A .craft names its part `part = mk1pod_42`, a VESSEL node `name = mk1pod`.
    # Both are collected and `part` wins: KSP ignores an unknown `name =` key in a
    # .craft PART node, so taking the first of the two let a stray line ahead of
    # `part =` rename the part for the hash without touching the craft.
    cur_part = ""
    cur_name = ""
    cur_pos = (0.0, 0.0, 0.0)
    pending = ""  # last bare token seen; a ConfigNode's node name precedes its "{"

    def flush():
        nonlocal cur_part, cur_name, cur_pos
        name = cur_part or cur_name
        if name:
            out.append((name, cur_pos))
        cur_part, cur_name, cur_pos = "", "", (0.0, 0.0, 0.0)

    for line in _preformat(text):
        if line == "{":
            depth += 1
            if part_depth is None and pending.upper() == "PART":
                part_depth = depth
                cur_part, cur_name, cur_pos = "", "", (0.0, 0.0, 0.0)
            pending = ""
            continue
        if line == "}":
            if part_depth is not None and depth == part_depth:
                flush()
                part_depth = None
            depth -= 1
            pending = ""
            continue

        m = _KEY.match(line)
        if not m:
            pending = line
            continue
        pending = ""
        # Only a PART node's own keys count — one level in is a MODULE.
        if part_depth is None or depth != part_depth:
            continue
        key, val = m.group(1), m.group(2)
        if key == "part" and not cur_part:
            cur_part = _PART_ID_SUFFIX.sub("", val.strip())
        elif key == "name" and not cur_name:
            cur_name = _PART_ID_SUFFIX.sub("", val.strip())
        elif key in ("pos", "position"):
            cur_pos = _vec(val)

    # A file truncated mid-node still yields the parts that closed cleanly; the
    # one left open is dropped rather than hashed half-read.
    return out


def _preformat(text: str) -> list[str]:
    """The lines KSP's ConfigNode reader actually sees.

    `ConfigNode.PreFormatConfig` strips `//` comments and splits every `{` and `}`
    onto a line of its own, which is what lets `PART {` (brace on the node's own
    line) and `} PART {` load unchanged. The scanner must see the same token
    stream, or a re-formatting that changes nothing KSP reads would leave it with
    no PART token before the `{` — zero parts, no design/parts hash, and every
    fuzzy ban matching nothing.
    """
    out: list[str] = []
    for raw in text.splitlines():
        cut = raw.find("//")
        if cut >= 0:
            raw = raw[:cut]
        buf = ""
        for ch in raw:
            if ch in "{}":
                if buf.strip():
                    out.append(buf.strip())
                out.append(ch)
                buf = ""
            else:
                buf += ch
        if buf.strip():
            out.append(buf.strip())
    return out


def _mentions_part_node(text: str) -> bool:
    """Whether the payload carries a PART node token at all — the difference
    between "not a craft" and "a craft this scanner could not read"."""
    return any(line.upper() == "PART" for line in _preformat(text))


def _vec(val: str) -> tuple[float, float, float]:
    """"x,y,z" → rounded floats. Rounded to the centimetre because the last
    decimals of a float are not evidence of anything: KSP re-serialises them, and
    the mod's TweakScale bake re-anchors surface-attached parts, so insisting on
    them would make `design` no more useful than `exact`."""
    try:
        nums = [float(p) for p in val.split(",")[:3]]
    except ValueError:
        return (0.0, 0.0, 0.0)
    while len(nums) < 3:
        nums.append(0.0)
    # +0.0 normalises -0.0, which formats differently and would split the hash.
    return tuple(round(n, 2) + 0.0 for n in nums)  # type: ignore[return-value]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def fingerprint(data: bytes) -> dict:
    """Every fingerprint of one craft, plus what they were derived from.

    `design` and `parts` are None for a payload no part could be read out of (an
    empty upload, a format this doesn't know). That is the honest answer and the
    callers rely on it: a fingerprint that silently fell back to hashing nothing
    would make one ban match every unparseable upload.
    """
    if len(data) > _MAX_FINGERPRINT_BYTES:
        # Too large to scan safely — hash the bytes and stop. Do NOT parse:
        # that is exactly the work an oversized payload is trying to make us do.
        log.warning("craft fingerprint: payload too large to scan (%d bytes), exact hash only", len(data))
        return {
            EXACT: hashlib.sha256(data).hexdigest(),
            DESIGN: None,
            PARTS: None,
            "part_count": 0,
            "distinct_parts": 0,
            "suspect": True,
        }
    text = data.decode("utf-8", "ignore")
    parts = _parts_of(text)
    fp = {
        EXACT: hashlib.sha256(data).hexdigest(),
        DESIGN: None,
        PARTS: None,
        "part_count": len(parts),
        "distinct_parts": len({n for n, _ in parts}),
        # A payload that names PART nodes but yielded none is not "not a craft" —
        # it is one the scanner could not read, which is exactly the shape a
        # ban-dodging edit has. Flagged rather than hashed, so a caller can log it
        # and a moderator can look, without one ban matching every odd upload.
        "suspect": False,
    }
    if not parts and _mentions_part_node(text):
        fp["suspect"] = True
        log.warning("craft fingerprint: payload has PART nodes but none could be read "
                    "(%d bytes), unusual formatting", len(data))
    if parts:
        fp[DESIGN] = _sha("\n".join(sorted(
            f"{n}|{p[0]:.2f},{p[1]:.2f},{p[2]:.2f}" for n, p in parts)))
        fp[PARTS] = _sha("\n".join(sorted(n for n, _ in parts)))
    return fp


def hash_list(fp: dict) -> list[str]:
    """A fingerprint as the "kind:hash" strings stored on a listing, so "which
    listings are this craft?" is one array-contains query instead of a scan over
    the whole market."""
    return [f"{k}:{fp[k]}" for k in KINDS if fp.get(k)]


# ══════════════════════════════════════════════════════════════════════════════
#  The ban list
# ══════════════════════════════════════════════════════════════════════════════

def _load(force: bool = False) -> dict[str, dict]:
    """The active bans, keyed by hash. Cached; returns the stale copy (or an
    empty one) when Firestore can't be reached — see the fail-open note above."""
    global _index, _index_at
    now = time.time()
    if not force and _index is not None and now - _index_at < _CACHE_TTL:
        return _index
    try:
        docs = _col().where("active", "==", True).stream()
        _index = {d.id: (d.to_dict() or {}) for d in docs}
        _index_at = now
    except Exception as exc:
        log.warning("Could not read craft bans (failing open): %s", exc)
        return _index if _index is not None else {}
    return _index


def check(data: bytes | None = None, fp: dict | None = None) -> dict | None:
    """The ban this craft trips, or None.

    Called on every craft upload, so it must stay cheap: one parse of the file
    and three dict lookups. Checked strictest-first, so a record that names the
    exact file wins over one that merely matches its parts bin — the reason the
    player is shown should be the most specific one there is.

    `fp` lets a caller that already needed the fingerprint for something else
    (the marketplace stores it on the listing) hand it over rather than pay for a
    second parse of the same file."""
    if fp is None:
        fp = fingerprint(data or b"")
    index = _load()
    if not index:
        return None
    for kind in KINDS:
        digest = fp.get(kind)
        if not digest:
            continue
        rec = index.get(digest)
        if rec and rec.get("kind") == kind and rec.get("active", True):
            return rec
    return None


def check_hashes(entries) -> dict | None:
    """The ban tripped by a stored "kind:hash" fingerprint list, or None.

    For the surfaces that hold a craft's fingerprint but not the craft — a
    marketplace listing being relisted, a moderator flipping one back to active.
    Same index as `check`, no download and no parse: the answer to "is this
    listing the banned craft" was written down when it was uploaded."""
    index = _load()
    if not index or not entries:
        return None
    for entry in entries:
        kind, _, digest = str(entry).partition(":")
        rec = index.get(digest)
        if rec and rec.get("kind") == kind and rec.get("active", True):
            return rec
    return None


def record_hit(rec: dict) -> None:
    """Count a refusal. Best-effort and deliberately not awaited on the refusal
    path: the upload is already refused, and a Storage/Firestore hiccup must not
    turn a working block into a 500. The count is what tells a moderator whether
    a ban is still doing anything a year later."""
    h = rec.get("hash")
    if not h:
        return
    stamp = _iso()
    rec["hits"] = int(rec.get("hits", 0) or 0) + 1
    rec["last_hit_at"] = stamp
    try:
        from firebase_admin import firestore
        _col().document(h).update({"hits": firestore.Increment(1), "last_hit_at": stamp})
    except Exception as exc:
        log.warning("Could not count craft-ban hit for %s: %s", h[:12], exc)


def refusal_message(rec: dict) -> str:
    """What the player is told. The stored reason verbatim — a moderator writing
    "this is someone else's craft" is more use than any wording we could add — or
    the default when the ban was issued without one."""
    return (rec.get("reason") or "").strip() or DEFAULT_REASON


def add_ban(digest: str, kind: str, reason: str, by: str,
            label: str = "", note: str = "", listing_id: str = "") -> dict:
    """Ban a hash. Re-banning an existing hash rewrites that one record (and
    revives a revoked one) rather than adding a second — the hash is the id."""
    digest = (digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("A craft hash is 64 hex characters (sha256).")
    if kind not in KINDS:
        raise ValueError(f"Unknown ban kind {kind!r}.")
    rec = {
        "hash": digest,
        "kind": kind,
        "reason": (reason or "").strip()[:REASON_MAX],
        "note": (note or "").strip()[:NOTE_MAX],
        "label": (label or "").strip()[:LABEL_MAX],
        "listing_id": (listing_id or "").strip(),
        "by": by,
        "created_at": _iso(),
        "active": True,
        "revoked_at": None,
        "revoked_by": None,
        "hits": 0,
        "last_hit_at": None,
    }
    _col().document(digest).set(rec)
    # Update the cache only when there IS one: seeding an unloaded index with the
    # single record just written would make it claim to be the whole ban list, and
    # every other ban would go unenforced until the TTL ran out.
    if _index is not None:
        _index[digest] = rec
    log.warning("Craft ban added by %s: %s %s (%s)", by, kind, digest[:12], rec["label"])
    return rec


def revoke(digest: str, by: str) -> bool:
    """Lift a ban. False when there was no active one to lift."""
    digest = (digest or "").strip().lower()
    try:
        snap = _col().document(digest).get()
    except Exception as exc:
        log.warning("Could not read craft ban %s: %s", digest[:12], exc)
        return False
    if not snap.exists:
        return False
    rec = snap.to_dict() or {}
    if not rec.get("active", True):
        return False
    rec.update({"active": False, "revoked_at": _iso(), "revoked_by": by})
    _col().document(digest).set(rec)
    if _index is not None:
        _index.pop(digest, None)
    log.warning("Craft ban revoked by %s: %s", by, digest[:12])
    return True


def list_bans(include_revoked: bool = True) -> list[dict]:
    """Every ban, newest first — the console's list. Unlike `check` this reads
    through to Firestore: a moderator looking at the page wants what is actually
    stored, including the revoked records the cache deliberately drops."""
    try:
        docs = [d.to_dict() or {} for d in _col().stream()]
    except Exception as exc:
        log.warning("Could not list craft bans: %s", exc)
        return []
    if not include_revoked:
        docs = [d for d in docs if d.get("active", True)]
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return docs


def invalidate() -> None:
    """Drop the cache — for tests and for a console action that wrote outside
    add_ban/revoke."""
    global _index, _index_at
    _index, _index_at = None, 0.0
