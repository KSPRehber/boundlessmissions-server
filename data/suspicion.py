"""
data/suspicion.py – Server-side anti-cheat flagging (Firestore record + dedupe).

The KSP client is untrusted: anything it reports can be forged. Reward-bearing
endpoints validate what they can server-side; when something still looks wrong
(a failed attestation, repeated illegal-mod submissions, impossible telemetry…)
they call `record()` here to log it, and use `claim_ticket()` to decide whether
to open a moderator ticket (deduped per user+reason so mods aren't spammed).

The ticket itself is opened by the API layer (api_server.flag_suspicion), which
has the Discord client; this module is pure Firestore so it stays import-cycle
free and runs off the event loop via asyncio.to_thread.

Firestore layout:
    guilds/{gid}/suspicions/{auto}              # immutable event log (audit trail)
    guilds/{gid}/suspicion_state/{uid}          # per-user counters + ticket cooldowns
"""

import time
import logging
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)


def _events_col(gid):
    return _db.collection("guilds").document(str(gid)).collection("suspicions")


def _state_doc(gid, uid):
    return _db.collection("guilds").document(str(gid)).collection("suspicion_state").document(str(uid))


def record(gid, uid, username: str, reason: str, severity: str, details: str) -> int:
    """Append a suspicion event and bump the per-user/per-reason counter.
    Returns the running count of this reason for this user (all-time)."""
    try:
        _events_col(gid).add({
            "user_id": str(uid),
            "username": username,
            "reason": reason,
            "severity": severity,
            "details": details[:1500],
            "at": datetime.now(timezone.utc).isoformat(),
            "ts": time.time(),
        })
    except Exception as exc:
        log.warning("Could not record suspicion (%s/%s): %s", uid, reason, exc)

    count = 0
    try:
        ref = _state_doc(gid, uid)
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {}
        counts = data.get("counts") or {}
        count = int(counts.get(reason, 0)) + 1
        counts[reason] = count
        ref.set({"counts": counts, "username": username,
                 "last_at": time.time()}, merge=True)
    except Exception as exc:
        log.warning("Could not bump suspicion counter (%s/%s): %s", uid, reason, exc)
    return count


def claim_ticket(gid, uid, reason: str, cooldown_seconds: float) -> bool:
    """Return True at most once per cooldown window for a given (user, reason),
    stamping the time so concurrent/rapid flags don't open duplicate tickets.

    The stamp is written *before* the caller has a ticket, which is the only order
    that can dedupe concurrent flags — so a caller whose ticket then fails to open
    must hand the claim back with `release_ticket`, or the cooldown it burned
    silences the next 12-24 hours of that signal for nothing.
    """
    try:
        ref = _state_doc(gid, uid)
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {}
        last_map = data.get("last_ticket") or {}
        now = time.time()
        if now - float(last_map.get(reason, 0)) < cooldown_seconds:
            return False
        last_map[reason] = now
        ref.set({"last_ticket": last_map}, merge=True)
        return True
    except Exception as exc:
        log.warning("Could not claim suspicion ticket (%s/%s): %s", uid, reason, exc)
        # Fail open: better a possible duplicate ticket than a silently dropped flag.
        return True


def release_ticket(gid, uid, reason: str) -> None:
    """Undo a `claim_ticket` whose ticket was never opened.

    The claim is stamped optimistically, because a stamp written after a
    successful `create_ticket` could not dedupe two flags racing each other. The
    cost of that order is this function: everything between the claim and the
    ticket can fail — the bot not connected yet, the guild not in cache,
    `create_ticket` returning None because the guild's hourly ticket budget is
    spent or the category is full — and every one of those paths used to lose the
    ticket AND the claim, so the next identical signal from the same player was
    refused by a cooldown that is paying for a ticket nobody ever saw. Filling a
    guild's ticket budget is cheap, which made it a way to silence anti-cheat.

    Clearing the stamp rather than back-dating it: the window is a dedupe of a
    ticket that exists, and no ticket exists. Best-effort and silent on failure —
    the worst case of a failed release is one duplicate ticket, which is the side
    this whole module already errs on (`claim_ticket` fails open for the same
    reason).
    """
    try:
        ref = _state_doc(gid, uid)
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {}
        last_map = data.get("last_ticket") or {}
        if reason not in last_map:
            return
        last_map.pop(reason, None)
        # `update` with a whole-map value REPLACES that field; `set(merge=True)`
        # would deep-merge it and the popped key would simply survive the write,
        # making the release a no-op (the same trap data/friends.py documents).
        # The other fields on this document — the counters, the username — are
        # untouched either way, which is why this is not a whole-document set.
        ref.update({"last_ticket": last_map})
    except Exception as exc:
        log.warning("Could not release suspicion ticket claim (%s/%s): %s", uid, reason, exc)
