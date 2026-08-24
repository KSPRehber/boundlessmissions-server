"""test_craft_bans.py – Behavioural tests for craft hash bans.

No network, no Firebase, no KSP: `data.store` is stubbed out before import so
`data/craft_bans.py` runs against a fake Firestore collection. The module under
test is the shipped one, not a re-implementation. The craft fixtures are real
KSP ConfigNode text, small but in the exact shape the game writes.

What is covered:
  [A] fingerprints      what each of the three survives, and what it must not
  [B] the ban list      add / kind isolation / revoke / re-ban
  [C] the cache         a write takes effect at once; reads don't re-query
  [D] failing open      a Firestore error is "not banned", never "banned"
  [E] stored hashes     check_hashes answers from a listing's own fingerprint
  [F] bounds            a malformed hash or kind is refused, not stored
  [G] hit counting      a refusal is counted on the record
  [H] enforcement       every craft-ingest path in api_server.py calls the gate

Run:  ./.venv/bin/python test_craft_bans.py
"""
import os
import sys
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

class FakeSnap:
    def __init__(self, data, doc_id=""):
        self._data, self.id = data, doc_id

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, col, doc_id):
        self.col, self.id = col, doc_id

    def get(self):
        self.col.reads += 1
        if self.col.fail:
            raise RuntimeError("firestore is down")
        return FakeSnap(self.col.docs.get(self.id), self.id)

    def set(self, data, merge=False):
        if self.col.fail:
            raise RuntimeError("firestore is down")
        self.col.docs[self.id] = dict(data)

    def update(self, fields):
        if self.col.fail:
            raise RuntimeError("firestore is down")
        doc = self.col.docs.setdefault(self.id, {})
        for k, v in fields.items():
            # Stand-in for firestore.Increment, which arrives here as an object.
            doc[k] = int(doc.get(k, 0) or 0) + 1 if k == "hits" else v


class FakeCol:
    def __init__(self):
        self.docs, self.reads, self.queries, self.fail = {}, 0, 0, False
        self._filter = None

    def document(self, doc_id):
        return FakeDoc(self, doc_id)

    def where(self, field, op, value):
        assert op == "==", op
        self._filter = (field, value)
        return self

    def stream(self):
        self.queries += 1
        if self.fail:
            raise RuntimeError("firestore is down")
        items = [FakeSnap(d, i) for i, d in self.docs.items()]
        if self._filter:
            field, value = self._filter
            items = [s for s in items if (s.to_dict() or {}).get(field) == value]
            self._filter = None
        return items


class FakeDb:
    def __init__(self, col):
        self._col = col

    def collection(self, name):
        assert name == "craft_bans", name
        return self._col


COL = FakeCol()
_stub = types.ModuleType("data.store")
_stub._db = FakeDb(COL)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data  # noqa: E402  (real package, for the submodule path)
sys.modules["data.store"] = _stub
data.store = _stub

from data import craft_bans as CB  # noqa: E402


def reset():
    COL.docs.clear()
    COL.reads = COL.queries = 0
    COL.fail = False
    CB.invalidate()


# ── Fixtures: real ConfigNode shapes ─────────────────────────────────────────

def craft(ship="Test Ship", desc="", parts=(("mk1pod", 0.0), ("fuelTank", -1.5),
                                            ("liquidEngine", -3.0))):
    """A .craft as KSP writes one: header keys, then PART nodes carrying a
    `part = <name>_<instance id>` and nested MODULE nodes whose own `name = ` must
    never be mistaken for a part."""
    out = [f"ship = {ship}", "version = 1.12.5", f"description = {desc}", "type = VAB"]
    for i, (name, y) in enumerate(parts):
        out += [
            "PART", "{",
            f"\tpart = {name}_{4294320818 + i}",
            "\tpartName = Part",
            f"\tpos = 0,{y},0",
            "\trot = 0,0,0,1",
            "\tMODULE", "\t{", "\t\tname = ModuleCommand",
            "\t\tisEnabled = True", "\t}",
            "\tRESOURCE", "{", "\t\tname = ElectricCharge", "\t\tamount = 150", "\t}",
            "}",
        ]
    return "\n".join(out).encode()


def vessel(parts=(("mk1pod", 0.0), ("fuelTank", -1.5), ("liquidEngine", -3.0))):
    """The same ship as a saved VESSEL node — the dialect a live-vessel quicksend
    and a flight submission carry, where a part is `name = ` and not `part = `."""
    out = ["VESSEL", "{", "\tpid = 3648805105", "\tname = Test Ship", "\ttype = Ship"]
    for name, y in parts:
        out += [
            "\tPART", "\t{",
            f"\t\tname = {name}",
            "\t\tuid = 1234567",
            f"\t\tposition = 0,{y},0",
            "\t\tMODULE", "\t\t{", "\t\t\tname = ModuleCommand", "\t\t}",
            "\t}",
        ]
    out.append("}")
    return "\n".join(out).encode()


# ── [A] fingerprints ──────────────────────────────────────────────────────────

print("\n[A] fingerprints")
base = CB.fingerprint(craft())
check("all three are produced for a readable craft",
      all(base[k] for k in CB.KINDS), base)
check("the parts are counted, and the MODULE names are not among them",
      (base["part_count"], base["distinct_parts"]) == (3, 3),
      f"{base['part_count']}/{base['distinct_parts']}")

renamed = CB.fingerprint(craft(ship="Definitely Mine", desc="my own work"))
check("a rename changes the exact hash", renamed[CB.EXACT] != base[CB.EXACT])
check("...but not the design hash", renamed[CB.DESIGN] == base[CB.DESIGN])

# The mod appends its side-channel blocks (GKFLAG/GKTU/GKMODS/GKTHUMB) on export.
reexported = CB.fingerprint(craft() + b"\nGKMODS\n{\n\tmod = Squad\n}\n")
check("a re-export past the GK blocks keeps the design hash",
      reexported[CB.DESIGN] == base[CB.DESIGN])

jitter = CB.fingerprint(craft(parts=(("mk1pod", 0.0000001), ("fuelTank", -1.499999),
                                     ("liquidEngine", -3.0))))
check("sub-centimetre float noise does not split the design hash",
      jitter[CB.DESIGN] == base[CB.DESIGN])

nudged = CB.fingerprint(craft(parts=(("mk1pod", 0.0), ("fuelTank", -1.9),
                                     ("liquidEngine", -3.0))))
check("moving a part by 40cm does change it", nudged[CB.DESIGN] != base[CB.DESIGN])
check("...while the parts hash still recognises the ship",
      nudged[CB.PARTS] == base[CB.PARTS])

other = CB.fingerprint(craft(parts=(("probeCoreOcto", 0.0), ("solarPanel", -0.5))))
check("a different craft shares no fingerprint",
      other[CB.DESIGN] != base[CB.DESIGN] and other[CB.PARTS] != base[CB.PARTS])

vfp = CB.fingerprint(vessel())
check("a VESSEL node parses too (the quicksend/flight dialect)",
      (vfp["part_count"], vfp["distinct_parts"]) == (3, 3),
      f"{vfp['part_count']}/{vfp['distinct_parts']}")
check("and yields the same parts hash as the .craft of the same ship",
      vfp[CB.PARTS] == base[CB.PARTS])

empty = CB.fingerprint(b"")
check("an unreadable payload gets NO design/parts hash (never a hash of nothing)",
      empty[CB.DESIGN] is None and empty[CB.PARTS] is None)
check("...but still gets an exact one", bool(empty[CB.EXACT]))
check("two unreadable payloads do not collide into one ban",
      CB.fingerprint(b"junk")[CB.EXACT] != empty[CB.EXACT])


# ── [B] the ban list ──────────────────────────────────────────────────────────

print("\n[B] ban / kind isolation / revoke")
reset()
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "reuploaded someone else's craft", "owner",
           label="Test Ship")
hit = CB.check(craft())
check("the banned craft is refused", hit is not None)
check("the moderator's reason is what the player is told",
      CB.refusal_message(hit) == "reuploaded someone else's craft")
check("a rename does not get past a design ban", CB.check(craft(ship="Mine")) is not None)
check("an unrelated craft is untouched",
      CB.check(craft(parts=(("probeCoreOcto", 0.0),))) is None)
check("the same ship as a live vessel is refused too", CB.check(vessel()) is not None)

reset()
CB.add_ban(base[CB.EXACT], CB.EXACT, "r", "owner")
check("an exact ban blocks the identical file", CB.check(craft()) is not None)
check("...and nothing else", CB.check(craft(ship="Renamed")) is None)

reset()
# A design hash stored under the wrong kind must not fire: the record says which
# question it is the answer to.
CB.add_ban(base[CB.DESIGN], CB.EXACT, "r", "owner")
check("a hash banned under the wrong kind never matches", CB.check(craft()) is None)

reset()
CB.add_ban(base[CB.PARTS], CB.PARTS, "r", "owner")
check("a parts ban survives the craft being rearranged",
      CB.check(craft(parts=(("mk1pod", 9.0), ("fuelTank", 4.0), ("liquidEngine", 1.0)))) is not None)

reset()
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "first", "owner")
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "second", "owner")
check("re-banning one craft keeps ONE record", len(COL.docs) == 1, f"{len(COL.docs)} docs")
check("...and the newer reason wins", CB.check(craft())["reason"] == "second")

check("revoking lets it through", CB.revoke(base[CB.DESIGN], "owner") and CB.check(craft()) is None)
check("the record survives the revoke (the audit trail)",
      COL.docs[base[CB.DESIGN]]["active"] is False)
check("revoking twice reports nothing to do", CB.revoke(base[CB.DESIGN], "owner") is False)
check("a revoked ban is still listed for the console",
      len(CB.list_bans()) == 1 and len(CB.list_bans(include_revoked=False)) == 0)


# ── [C] the cache ─────────────────────────────────────────────────────────────

print("\n[C] cache")
reset()
CB.check(craft())                       # cold: loads the list
before = COL.queries
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "r", "owner")
check("a ban takes effect at once, with no read of its own",
      CB.check(craft()) is not None and COL.queries == before,
      f"{COL.queries - before} queries")
CB.check(craft())
CB.check(craft(ship="Another"))
check("repeat checks are served from memory (no query per upload)",
      COL.queries == before, f"{COL.queries - before} queries")

reset()
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "r", "owner")
check("a write into an unloaded cache leaves it unloaded — a cache holding only "
      "the one ban just written would hide every other ban for a whole TTL",
      CB._index is None)
check("...and the next check loads the real list", CB.check(craft()) is not None)

reset()
CB.check(craft())          # cold: one query, empty result cached
first = COL.queries
CB.check(craft())
check("an empty ban list is cached too", COL.queries == first)


# ── [D] failing open ──────────────────────────────────────────────────────────

print("\n[D] failing open")
reset()
COL.fail = True
check("an unreachable Firestore blocks nothing", CB.check(craft()) is None)
check("...and does not poison the cache with that answer", CB._index is None)

reset()
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "r", "owner")
CB.check(craft())
COL.fail = True
CB._index_at = 0            # force a refresh while the database is down
check("a stale ban list is preferred over letting everything through",
      CB.check(craft()) is not None)


# ── [E] hashes stored on a listing ───────────────────────────────────────────

print("\n[E] stored fingerprints")
reset()
entries = CB.hash_list(base)
check("hash_list is kind-prefixed and covers all three",
      entries == [f"{k}:{base[k]}" for k in CB.KINDS], entries)
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "r", "owner")
check("a listing is recognised from its own stored hashes",
      CB.check_hashes(entries) is not None)
check("a listing of something else is not",
      CB.check_hashes(CB.hash_list(other)) is None)
check("no stored hashes (an old listing) is not a match", CB.check_hashes([]) is None)
check("a hash stored under the wrong kind still doesn't match",
      CB.check_hashes([f"exact:{base[CB.DESIGN]}"]) is None)


# ── [F] bounds ────────────────────────────────────────────────────────────────

print("\n[F] bounds")
reset()


def raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


check("a non-hex hash is refused", raises(lambda: CB.add_ban("nope", CB.DESIGN, "", "o")))
check("a truncated hash is refused", raises(lambda: CB.add_ban("ab" * 20, CB.DESIGN, "", "o")))
check("an unknown kind is refused", raises(lambda: CB.add_ban(base[CB.EXACT], "vibes", "", "o")))
check("nothing was stored by the refusals", len(COL.docs) == 0, f"{len(COL.docs)} docs")
long_reason = "x" * 900
rec = CB.add_ban(base[CB.EXACT], CB.EXACT, long_reason, "owner", label="y" * 500)
check("the reason is capped", len(rec["reason"]) == CB.REASON_MAX)
check("the label is capped", len(rec["label"]) == CB.LABEL_MAX)
check("a ban issued with no reason still says something",
      CB.refusal_message({"reason": ""}) == CB.DEFAULT_REASON)


# ── [G] hit counting ──────────────────────────────────────────────────────────

print("\n[G] hits")
reset()
CB.add_ban(base[CB.DESIGN], CB.DESIGN, "r", "owner")
rec = CB.check(craft())
CB.record_hit(rec)
CB.record_hit(rec)
check("refusals are counted on the record", COL.docs[base[CB.DESIGN]]["hits"] == 2,
      COL.docs[base[CB.DESIGN]].get("hits"))
check("...and on the cached copy the console reads", CB.check(craft())["hits"] == 2)
COL.fail = True
CB.record_hit(rec)          # must not raise: the upload is already refused
check("a counting failure never breaks the refusal", True)


# ── [H] enforcement points ────────────────────────────────────────────────────
#
# A source check rather than a behavioural one: standing up FastAPI + Firebase to
# assert "this endpoint calls the gate" would test the harness, not the rule. The
# rule is that no path may take a craft from a client without asking.

print("\n[H] enforcement")
src = open(os.path.join(HERE, "api_server.py"), encoding="utf-8").read()
for label, marker in (
    ("marketplace listing", 'fp=craft_fp'),
    ("quicksend", 'f"quicksend ({kind})"'),
    ("contract submission", 'ban_payloads'),
    ("relist", 'cbans.check_hashes, listing.get("craft_hashes")'),
):
    check(f"{label} goes through the ban gate", marker in src)
check("the gate fails open in the server too",
      "Craft ban check failed (letting it through)" in src)
check("a listing records its fingerprint at upload", "craft_hashes=cbans.hash_list" in src)
# The sweep is an array-contains query, so it can only see listings that carry a
# fingerprint. The listing a ban is issued FROM may predate that, and it is the
# one listing the ban must never fail to take down.
check("banning from a listing stores its fingerprint before the sweep",
      "_remember_hashes" in src
      and src.index("_remember_hashes, listing, fp") < src.index("mkt.list_by_hash, f\"{kind}:{digest}\""))
check("a seller is told when a ban takes their listing down",
      '"marketplace_banned"' in src)
check("craft bans are owner-only, not guild-admin",
      src.count('@app.post("/api/v1/web/admin/craftbans")') == 1
      and "async def admin_add_craft_ban(req: AdminCraftBan, user: dict = Depends(get_owner))" in src)


print(f"\n{'─' * 60}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
