"""data/suspensions — the cache, the caps, and what a read failure does on lift."""
import math
import time

from _h import check, section, finish, FakeCol

from data import suspensions as S

col = FakeCol()
S._col = lambda: col
S._cache.clear()
U = "42"

section("caps")
rec = S.suspend(U, 10 ** 9, "x", "owner")
check("hours above MAX_HOURS clamp to a year", rec["hours"] == S.MAX_HOURS and
      rec["until"] <= time.time() + S.MAX_HOURS * 3600 + 1)
rec = S.suspend(U, float("nan"), "x", "owner")
check("NaN hours -> MIN_HOURS, finite until", rec["hours"] == S.MIN_HOURS and math.isfinite(rec["until"]))
rec = S.suspend(U, float("inf"), "x", "owner")
check("inf hours -> clamped, finite until", math.isfinite(rec["until"]) and rec["hours"] == S.MIN_HOURS)
rec = S.suspend(U, 0, "x", "owner")
check("0 hours -> MIN_HOURS (cannot mint an already-expired record)", rec["hours"] == S.MIN_HOURS)

section("cache never outlives the suspension")
S.suspend(U, 1, "x", "owner")
S._cache[U] = (dict(col.docs[U], until=time.time() + 0.3), time.time())
check("cached and active", S.get_active(U) is not None)
col.docs[U]["until"] = 0            # the DB says it ended
time.sleep(0.4)
check("past the cached until, the record is re-read and is expired", S.get_active(U) is None)

section("lift takes effect immediately in the cache")
S.suspend(U, 5, "x", "owner")
check("active after suspend", S.get_active(U) is not None)
check("lift returns True", S.lift(U, "owner") is True)
check("inactive right after lift (no TTL wait)", S.get_active(U) is None)
check("lift on nothing running returns False", S.lift(U, "owner") is False)

section("a failed read during lift")
S._cache.clear()
S.suspend(U, 5, "x", "owner")
real_document = col.document
class Boom:
    def __init__(self, d): self._d = d
    def get(self): raise RuntimeError("firestore unavailable")
    def set(self, *a, **k): return self._d.set(*a, **k)
col.document = lambda k: Boom(real_document(k))
lifted = S.lift(U, "owner")
col.document = real_document
check("lift with an unreadable record reports 'nothing running'", lifted is False,
      "(informational — it says False rather than raising)")
still = S._active(col.docs[U], time.time()) is not None
check("a lift that could not read the record does not cache 'not suspended'",
      not (still and S.get_active(U) is None),
      "DB record is still active, but get_active() answers None from the (None, now) entry "
      "lift wrote — 30 s of access, and the console was told nothing was running")

section("a failed read on the gate itself fails open but does not cache")
S._cache.clear()
col.document = lambda k: Boom(real_document(k))
check("get_active fails open", S.get_active(U) is None)
check("…and caches nothing", U not in S._cache)
col.document = real_document
check("next read sees the real record again", S.get_active(U) is not None)
finish()
