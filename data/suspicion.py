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
    stamping the time so concurrent/rapid flags don't open duplicate tickets."""
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
