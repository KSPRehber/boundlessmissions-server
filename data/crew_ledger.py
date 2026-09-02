"""
data/crew_ledger.py – which of a player's own kerbals are out on loan, and to whom.

`VesselTransfer.ApplyIncomingOwnershipTag` refuses an incoming crew name that
claims to be ours. That refusal is the RM1 impersonation defence and must not be
weakened: the crew node is written entirely by the sender, so "{me}'s Jeb"
arriving from somebody else is a *claim*, not a fact — an attacker who renamed
themselves to a victim would otherwise have the victim's own roster entries
adopted onto the arriving hull, where the next hand-over deletes them.

The cost of that refusal is paid by the honest return leg, which has exactly the
forgery's shape. A rescue escapes it because the server has independent evidence:
the `rescue_kerbals` the issuer's own client tagged when it handed the wreck over,
held server-side ever since, and offered back to that issuer as `homebound` — the
one list a counterparty cannot write. A **quicksend had no such record**, so
lending a crewed ship to a friend and getting it back cost a player their crew's
identity permanently: the kerbals returned double-tagged and `borrowed = True`,
the originals gone from the roster, and borrowed crew are eligible for
`PurgeBorrowedGhostCrew` — so they could later be deleted outright (§3.11 of
`0109_ingame_verification.md`, verified end to end across two accounts).

This module is that missing record. It is deliberately the *same shape* as the
rescue evidence rather than a second mechanism: the server writes down which of
the sender's own crew left their save and who received them, and on a live-vessel
send back to that original owner it offers exactly those names as `homebound`.
Attested returns strip their tag; everything else keeps the current refusal
untouched.

Six things are load-bearing.

**Only bare names are recorded.** A name arriving here already carrying "{x}'s "
is somebody else's kerbal riding along on the sender's ship — a borrowed
passenger, or an honest multi-hop — and the sender is not owed it back as their
own. `IsBorrowedCrewName` draws the same line client-side, and recording a tagged
name would let A launder C's kerbal into A's roster by way of B.

**The ledger is keyed on the owner, and the holder is a field inside it** — one
document per player, not one per pair, which is the trade `data/friends.py` and
`marketplace_votes/{user_id}` make for the same reason: this project's
`cost_guard` exists because Firestore operations are the bill being defended
against. A live-vessel quicksend costs one extra write (record the outbound) and
one extra read (the recipient's ledger, to answer the return). Nothing else in
the codebase reads it, so a blueprint send, a marketplace buy and every contract
path are untouched.

**Attestation is per (owner, holder) pair, never per owner.** Only the player a
kerbal was actually handed to may hand them back. Keying on the owner alone would
let anyone who merely *learned* the names return them, which is the forgery again
with an extra step. A multi-hop return (A → B → C → A) is therefore still
refused; that is a known, deliberate gap and the conservative direction — it
costs an ownership tag, where the wrong answer costs a kerbal.

**Entries are not consumed on return, only expired.** Consuming looks tidier and
breaks the decline path: A returns a ship to B, B declines it, it comes back to
A, and a consumed entry would leave the next honest return unattested. Re-
attestation is safe in a way consumption is not — the only thing it can ever do
is let A's own name come home to A — so the ledger expires on a clock
(`CREW_LEDGER_TTL_DAYS`) instead, pruned lazily on read so a stale entry costs a
write only when one is already being made.

**A failed read fails open** — no attestation, never an exception. This does not
gate anything: with no record the return simply takes the refusal it takes today,
which is the pre-existing behaviour and not a new failure. Raising instead would
turn a Firestore blip into a refused hand-over, which is strictly worse.

**It is written at send time, not at accept time.** The send is the moment the
sender's client removes the crew from their save, which is the event being
recorded. An offer the recipient later declines leaves a harmless entry: it only
ever permits a return *from that recipient*, who never received the ship.

Document shape (`crew_handovers/{owner_account_id}`):

    {
      "out": { "<holder_account_id>": { "<bare kerbal name>": <epoch> } },
      "updated": <epoch>,
    }
"""
import logging
import time
from typing import Iterable

from firebase_admin import firestore
# The escaping half of a Firestore field path. Built from a LIST of segments, never
# from an f-string: a field path is dotted, and a kerbal name is arbitrary player text.
# "Bob Jr. Kerman" written as f"out.{hid}.{n}" re-parses into FOUR segments and deletes
# nothing, which fails this module open — see the note on _field_path below.
from google.cloud.firestore_v1.field_path import render_field_path

import settings
from data.store import _db

log = logging.getLogger(__name__)


def _col():
    return _db.collection("crew_handovers")


def _now() -> float:
    return time.time()


def _ttl_seconds() -> float:
    return float(getattr(settings, "CREW_LEDGER_TTL_DAYS", 90)) * 86400.0


def _is_borrowed(name: str) -> bool:
    """The server's copy of `VesselTransfer.IsBorrowedCrewName`: a roster name still
    carrying an ownership tag belongs to somebody else. Kept as its own function so
    the one rule the two ends must agree on is named on both."""
    return "'s " in name


def strip_tag(name: str) -> str:
    """"{owner}'s {Name}" -> "Name"; an untagged name unchanged. The server's copy of
    `VesselTransfer.StripOwnershipTag`, used to read a *returning* payload's crew back
    to the bare names the outbound leg recorded — so neither side's display name
    having changed in between can break the match."""
    idx = name.find("'s ")
    return name[idx + 3:] if idx > 0 else name


# The most crew names one hand-over may record. Mirrors the mod's own
# `VesselTransfer.MaxCrewPerVessel` (200): that is what a real ship can seat, and
# the names arrive from a client-authored vessel node.
MAX_RECORDED_CREW = 200


def record_handover(owner_id, holder_id, crew_names: Iterable[str]) -> int:
    """Write down that `owner_id`'s own crew left their save to `holder_id`.

    Returns how many names were recorded (0 when the payload carried none of the
    sender's own crew, which is every uncrewed ship and every ship carrying only
    borrowed passengers — neither costs a write).
    """
    names = {n.strip() for n in (crew_names or []) if n and n.strip()}
    mine = sorted(n for n in names if not _is_borrowed(n))
    if not mine:
        return 0
    # Bounded, because these names become Firestore MAP KEYS in one document and
    # they come from the uploaded vessel node — a client artefact whose only limits
    # are `MAX_UPLOAD_BYTES` and the daily upload quota. A payload of `crew = X`
    # lines yields hundreds of thousands of names, and while Firestore refuses the
    # oversized write (so nothing corrupts), the document grows toward its 1 MiB
    # ceiling across sends and is re-read on every live-vessel quicksend to that
    # friend. The bound mirrors the mod's own `MaxCrewPerVessel`, which is the
    # honest producer's ceiling; anything past it is not a roster.
    if len(mine) > MAX_RECORDED_CREW:
        log.warning("Crew handover from %s to %s declared %d names; recording the "
                    "first %d.", owner_id, holder_id, len(mine), MAX_RECORDED_CREW)
        mine = mine[:MAX_RECORDED_CREW]

    oid, hid = str(owner_id), str(holder_id)
    now = _now()
    try:
        # A blind merge, so the outbound leg costs exactly one operation. Firestore's
        # merge deep-merges nested maps, which is what is wanted here (add these
        # names, keep the rest) and is the very thing `data/friends.py` must avoid —
        # there a merge would make every unfriend a no-op, here it is the mechanism.
        _col().document(oid).set(
            {"out": {hid: {n: now for n in mine}}, "updated": now},
            merge=True,
        )
    except Exception as exc:
        # Never fatal: the hand-over itself has already been authorised and paid
        # for, and a missing record costs an ownership tag on a later return, not
        # the ship. Logged loudly because it is exactly what §3.11 is about.
        log.warning("Crew handover ledger write failed (%s -> %s): %s", oid, hid, exc)
        return 0
    return len(mine)


def consume_homebound(owner_id, holder_id, attested: Iterable[str]) -> int:
    """Spend the attestation for `attested` — the owner's crew have come home.

    An entry used to be expired and never consumed, on the reasoning that
    re-attestation "can only ever let A's own name come home to A". That is true of
    the ownership TAG but not of everything the attestation buys: a `homebound` name
    is also exempted from `ResolveIncomingCrewName`'s rename-aside, and
    `AddCrewToRosterInner` then keys the arriving vessel onto the roster entry that
    is already there. So a holder did not have to be returning the ship they were
    lent, or any related ship — for the whole TTL they could put ANY live vessel's
    crew node in front of the owner naming a remembered kerbal, and the owner's own
    (free, unassigned) kerbal was adopted onto a hull of the holder's choosing. If
    the owner later handed that hull on, `CrewFate.LeavesWithCraft` removes its crew
    from the roster for good.

    Single use closes that: an attestation is spent by the return it was written for.
    A later claim on the same name takes the ordinary refusal, which costs an
    ownership tag rather than a kerbal — the conservative direction this module
    already chooses for a multi-hop return.

    `restore_homebound` puts it back when the return is DECLINED, because then the
    holder still has them and is still owed the ability to send them home.
    """
    names = {strip_tag(n.strip()) for n in (attested or []) if n and n.strip()}
    names.discard("")
    if not names:
        return 0
    oid, hid = str(owner_id), str(holder_id)
    try:
        _col().document(oid).update(
            {_field_path("out", hid, n): firestore.DELETE_FIELD for n in names})
    except Exception as exc:
        # Fails open like every other read here: a spent attestation that could not
        # be deleted is the pre-existing behaviour, not a new hazard.
        log.warning("Could not consume crew attestation (%s -> %s): %s", oid, hid, exc)
        return 0
    return len(names)


def restore_homebound(owner_id, holder_id, attested: Iterable[str]) -> int:
    """Put back an attestation consumed for a return that was then declined.

    The vessel goes back to the holder, so they still hold the owner's crew and must
    still be able to bring them home later. Without this, one decline would burn the
    attestation and the honest second attempt would take the impersonation refusal —
    which is exactly the regression that made an honest rescue return delete the
    issuer's kerbals, and must not be reintroduced from the other side.

    The names are STRIPPED first, and that is load-bearing rather than tidiness:
    `attested` holds the names as they arrived on the returning vessel, which are
    tagged ("{holder}'s Jeb"), and `record_handover` deliberately drops tagged names
    as somebody else's kerbal riding along. Passing them through unstripped made this
    function a silent no-op — the restore reported success and restored nothing.
    """
    bare = [strip_tag(n) for n in (attested or []) if n and n.strip()]
    return record_handover(owner_id, holder_id, bare)


def homebound_for(owner_id, holder_id, incoming_names: Iterable[str]) -> list[str]:
    """Of `incoming_names` on a vessel `holder_id` is sending to `owner_id`, the ones
    the ledger attests are `owner_id`'s own crew coming home.

    Returned as the *incoming* spellings, because that is what the client matches on
    (`VesselTransfer.HomeboundSet` adds the stripped forms itself). Empty list when
    nothing is attested — which is not an error and is the status quo, so callers
    must not treat it as one.
    """
    names = [n.strip() for n in (incoming_names or []) if n and n.strip()]
    if not names:
        return []

    oid, hid = str(owner_id), str(holder_id)
    try:
        snap = _col().document(oid).get()
    except Exception as exc:
        # Fails OPEN — see the module docstring. No attestation is the behaviour
        # every quicksend had before this module existed.
        log.warning("Crew handover ledger read failed for %s: %s", oid, exc)
        return []
    if not snap.exists:
        return []

    rec = snap.to_dict() or {}
    out = rec.get("out")
    lent = (out or {}).get(hid) if isinstance(out, dict) else None
    if not isinstance(lent, dict) or not lent:
        return []

    cutoff = _now() - _ttl_seconds()
    fresh = {n for n, at in lent.items() if _as_epoch(at) >= cutoff}
    if len(fresh) != len(lent):
        _prune(oid, hid, lent, fresh)
    if not fresh:
        return []

    # Match on the *bare* name at both ends. The outbound leg recorded "Valentina
    # Kerman"; the return carries whatever the holder's client tagged it with
    # ("KSPRehber's Valentina Kerman"), and a display name either player changed in
    # between must not be able to break the match — which is why the pairing is
    # decided by the account ids above and the name comparison is on the core alone.
    return [n for n in names if strip_tag(n) in fresh]


def _as_epoch(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        # An unreadable timestamp is treated as expired rather than as fresh: the
        # cost is an unattested return (today's behaviour), where the other
        # direction would make a malformed entry immortal.
        return 0.0


def _field_path(*segments: str) -> str:
    """Render `segments` as one Firestore field path, escaping each segment.

    Every dotted path in this module goes through here, because the two segments
    below it are of completely different kinds and only one of them is safe. A
    holder id is a server-issued account id; a kerbal NAME is arbitrary player text
    that may contain spaces, apostrophes, backticks and — the case that actually
    bites — a full stop. "Bob Jr. Kerman" interpolated into an f-string re-parses as
    the four-segment path out / a_1 / `Bob Jr` / ` Kerman`, which matches no field,
    so `update()` deletes nothing and reports success. That would leave the
    attestation live: exactly the MB3 replay window single-use consumption exists to
    close, restored silently for any player whose kerbal has a dot in its name.

    `render_field_path` is the same function the client itself uses to serialise a
    FieldPath, so the string produced here is what the SDK would have produced.
    """
    return render_field_path(list(segments))


def _prune(owner_id: str, holder_id: str, lent: dict, fresh: set) -> None:
    """Drop expired names for one holder. Lazy, on the read that noticed them, so the
    ledger needs no sweeper — the same choice `data/suspensions.py` makes in resolving
    expiry on read rather than running a job to do it.

    Written as delete-the-holder then re-merge-the-survivors rather than as one
    `update()` naming each stale name, because a Firestore field path is dotted and a
    kerbal name is arbitrary user text containing spaces and apostrophes — the holder
    id is a server-issued account id and is the only segment safe to address directly.
    The order is deliberate: interrupted after the delete, the ledger has simply
    forgotten a loan, which costs an ownership tag on a later return and is exactly
    the behaviour every quicksend had before this module existed. The reverse order
    could leave an expired name live.
    """
    path = _field_path("out", holder_id)
    try:
        _col().document(owner_id).update({path: firestore.DELETE_FIELD})
        if fresh:
            _col().document(owner_id).set(
                {"out": {holder_id: {n: lent[n] for n in fresh}}, "updated": _now()},
                merge=True,
            )
    except Exception as exc:
        log.debug("Crew handover ledger prune failed for %s: %s", owner_id, exc)


def forget_account(owner_id) -> None:
    """Drop a player's whole ledger. Called from the Discord-side data purge, for the
    same reason `data/friends.py` has one: this records who someone lent kerbals to,
    which is a record *about them* and goes when they do."""
    try:
        _col().document(str(owner_id)).delete()
    except Exception as exc:
        log.warning("Crew handover ledger purge failed for %s: %s", owner_id, exc)
