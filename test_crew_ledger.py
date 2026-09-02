"""test_crew_ledger.py – Behavioural tests for the crew hand-over ledger (§3.11).

No network, no Firebase: `data.store` and `firebase_admin` are stubbed out before
import, exactly as `test_friends.py` does, so `data/crew_ledger.py` runs against a
fake Firestore. The fake models the one Firestore behaviour this module actually
leans on — `set(merge=True)` DEEP-merges nested maps — because that is the whole
mechanism here (add these names, keep the rest) and is the very thing
`data/friends.py` must avoid. A fake that shallow-merged would pass this file and
lose every previously recorded name in production.

These are a regression guard, not the primary evidence. The module was first
exercised against real Firestore and then end to end through two running copies of
KSP (harness scenario T8, `tools/gkbridge/scenarios.py`), which is what actually
establishes that a lent kerbal comes home under their own name.

What is covered:
  [A] recording        bare names only; a tagged passenger is somebody else's
  [B] attestation      the holder's return is attested, in the incoming spelling
  [C] refusals         a third party, a name never lent, and the reverse direction
  [D] renames          neither side's display name can break the match
  [E] expiry           the TTL drops an attestation, and the prune actually deletes
  [F] purge            forget_account leaves nothing behind
  [G] cost             the shape the cost_guard trade depends on

Run:  ./.venv/bin/python test_crew_ledger.py
"""
import os
import sys
import time
import types

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


# ── Fake Firestore ────────────────────────────────────────────────────────────

DELETE_SENTINEL = object()
OPS = {"read": 0, "write": 0}


def _deep_merge(dst, src):
    """Firestore's merge=True semantics for nested maps, which is the behaviour the
    module is built on. A DELETE_FIELD value removes the key."""
    for k, v in src.items():
        if v is DELETE_SENTINEL:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def get(self):
        OPS["read"] += 1
        return FakeSnap(self.store.get(self.key))

    def set(self, data, merge=False):
        OPS["write"] += 1
        if merge and self.key in self.store:
            _deep_merge(self.store[self.key], data)
        else:
            self.store[self.key] = _copy(data)

    def update(self, data):
        OPS["write"] += 1
        if self.key not in self.store:
            raise KeyError("no such document")
        for path, value in data.items():
            # Only the dotted, backtick-quoted form the module uses.
            parts = [p.strip("`") for p in path.split(".")]
            node = self.store[self.key]
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            if value is DELETE_SENTINEL:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = value

    def delete(self):
        OPS["write"] += 1
        self.store.pop(self.key, None)


def _copy(d):
    return {k: (_copy(v) if isinstance(v, dict) else v) for k, v in d.items()}


class FakeCollection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return FakeDoc(self.store, key)


class FakeDb:
    def __init__(self):
        self.cols = {}

    def collection(self, name):
        return FakeCollection(self.cols.setdefault(name, {}))


DB = FakeDb()

fake_store = types.ModuleType("data.store")
fake_store._db = DB
fake_firestore = types.ModuleType("firebase_admin.firestore")
fake_firestore.DELETE_FIELD = DELETE_SENTINEL
fake_admin = types.ModuleType("firebase_admin")
fake_admin.firestore = fake_firestore

sys.modules["firebase_admin"] = fake_admin
sys.modules["firebase_admin.firestore"] = fake_firestore
sys.modules.setdefault("data", types.ModuleType("data")).__path__ = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")]
sys.modules["data.store"] = fake_store

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings                                     # noqa: E402
from data import crew_ledger as cl                  # noqa: E402

A, B, C = "owner_A", "holder_B", "third_C"

# ── [A] recording ────────────────────────────────────────────────────────────
print("\n[A] recording")
n = cl.record_handover(A, B, ["Valentina Kerman", "Jebediah Kerman", "C's Bob Kerman"])
check("only the sender's OWN (untagged) crew are recorded", n == 2, f"recorded {n}")
stored = DB.cols["crew_handovers"][A]["out"][B]
check("a borrowed passenger is not recorded as the sender's",
      set(stored) == {"Valentina Kerman", "Jebediah Kerman"}, str(set(stored)))
check("an uncrewed send records nothing", cl.record_handover(A, C, []) == 0)
check("a send carrying only borrowed crew records nothing",
      cl.record_handover(A, C, ["D's Bob Kerman"]) == 0)
check("...and so writes no document at all", C not in DB.cols["crew_handovers"][A]["out"])

# ── [B] attestation ──────────────────────────────────────────────────────────
print("\n[B] attestation")
incoming = ["KSPRehber's Valentina Kerman", "KSPRehber's Jebediah Kerman", "Bill Kerman"]
got = cl.homebound_for(A, B, incoming)
check("the holder's return attests the names they were lent",
      sorted(got) == sorted(incoming[:2]), str(got))
check("the attestation is in the INCOMING spelling (what the client matches on)",
      all(g in incoming for g in got), str(got))
check("a bare incoming name matches too (the restore spelling)",
      cl.homebound_for(A, B, ["Valentina Kerman"]) == ["Valentina Kerman"])

# ── [C] refusals ─────────────────────────────────────────────────────────────
print("\n[C] refusals")
check("a THIRD party returning the same names attests nothing",
      cl.homebound_for(A, C, incoming) == [], "multi-hop must keep the refusal")
check("a name never lent is not attested",
      cl.homebound_for(A, B, ["X's Bill Kerman"]) == [])
check("direction matters — B has lent A nothing",
      cl.homebound_for(B, A, incoming) == [])
check("an unknown owner attests nothing", cl.homebound_for("nobody", B, incoming) == [])

# ── [D] renames ──────────────────────────────────────────────────────────────
print("\n[D] display-name changes")
check("a changed tag on the returning payload still matches",
      cl.homebound_for(A, B, ["SomethingElse's Valentina Kerman"])
      == ["SomethingElse's Valentina Kerman"],
      "the pairing is decided by account id, so only the core name is compared")

# ── [E] expiry ───────────────────────────────────────────────────────────────
print("\n[E] expiry")
old_ttl = settings.CREW_LEDGER_TTL_DAYS
settings.CREW_LEDGER_TTL_DAYS = -1
check("an expired loan attests nothing", cl.homebound_for(A, B, incoming) == [])
settings.CREW_LEDGER_TTL_DAYS = old_ttl
check("the prune actually removed it, rather than only filtering the read",
      cl.homebound_for(A, B, incoming) == [],
      "a filter-only expiry would come back the moment the TTL was raised")
check("...and the holder's whole entry is gone",
      B not in DB.cols["crew_handovers"][A].get("out", {}))

cl.record_handover(A, B, ["Valentina Kerman", "Jebediah Kerman"])
DB.cols["crew_handovers"][A]["out"][B]["Jebediah Kerman"] = 0.0   # one stale name
check("a partial expiry keeps the fresh name",
      cl.homebound_for(A, B, incoming) == ["KSPRehber's Valentina Kerman"])
check("...and drops only the stale one",
      set(DB.cols["crew_handovers"][A]["out"][B]) == {"Valentina Kerman"},
      str(DB.cols["crew_handovers"][A]["out"][B]))
check("an unreadable timestamp is treated as expired, never as immortal",
      cl._as_epoch("not-a-number") == 0.0)

# ── [F] purge ────────────────────────────────────────────────────────────────
print("\n[F] purge")
cl.forget_account(A)
check("forget_account leaves nothing", A not in DB.cols["crew_handovers"])
check("...and a later return attests nothing", cl.homebound_for(A, B, incoming) == [])

# ── [G] cost ─────────────────────────────────────────────────────────────────
print("\n[G] the cost_guard trade")
OPS["read"] = OPS["write"] = 0
cl.record_handover(A, B, ["Valentina Kerman"])
check("recording an outbound leg is ONE write and no read",
      (OPS["write"], OPS["read"]) == (1, 0), str(OPS))
OPS["read"] = OPS["write"] = 0
cl.homebound_for(A, B, ["KSPRehber's Valentina Kerman"])
check("answering a return is ONE read and no write when nothing is stale",
      (OPS["read"], OPS["write"]) == (1, 0), str(OPS))

# ── failing open ─────────────────────────────────────────────────────────────
print("\n[H] failing open")
boom = types.SimpleNamespace(get=lambda: (_ for _ in ()).throw(RuntimeError("firestore down")))
real_col = cl._col
cl._col = lambda: types.SimpleNamespace(document=lambda k: boom)
check("a failed read attests nothing rather than raising",
      cl.homebound_for(A, B, incoming) == [],
      "this gates nothing — with no record the return simply takes today's refusal")
cl._col = real_col

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
