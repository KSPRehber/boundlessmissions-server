#!/usr/bin/env python
"""repair_guild_zero.py - move records stranded on guild "0" to the real home guild.

WHY THEY EXIST
--------------
`_account_guild_id()` returns `str(cfg.HOME_GUILD_ID or 0)`, and "0" is deliberately
not a real guild: every guild-scoped lookup then resolves to nothing rather than to
somebody else's server. Production ran without `HOME_GUILD_ID` set, so every website
account minted in that window, and every KSP session it issued, was filed under "0".

The visible symptoms were:

  * the contract/quicksend picker showed nobody, because `list_corps` reads
    `guilds/{gid}/corps` and the only corp there was the caller's own;
  * bug reports produced no ticket, because `bug_report` does
    `_bot_instance.get_guild(0)`, gets None, and returns before `create_ticket`.

WHAT THIS DOES
--------------
Moves, for the guild "0" records only:

  guilds/0/corps/*           -> guilds/{home}/corps/*
  guilds/0/part_catalogs/*   -> guilds/{home}/part_catalogs/*
  guilds/0/ksp_notifications/*, guilds/0/ksp_craft_imports/*  (same, if any)
  corp_owners/*   guild_id "0" -> "{home}"
  ksp_sessions/*  guild_id "0" -> "{home}", ksp_guild_id "0" -> "{home}"
  contracts/*     guild_id "0" -> "{home}"

ORDER MATTERS. Set HOME_GUILD_ID in production's .env and restart the bot BEFORE
running this, or new records keep landing on "0" while you repair the old ones.

SAFETY
------
  * Dry run by default. Nothing is written without --apply.
  * Every document it will touch is written to a timestamped JSON backup first,
    including the destination's prior contents where one already existed.
  * Copy, verify the read-back, and only then delete the source. A failed verify
    leaves the source in place.
  * A destination that already exists is SKIPPED, never overwritten: an account
    with a corp in the home guild already is the state we are trying to reach.
  * Idempotent. Running it twice is a no-op.

USAGE
-----
    ./.venv/bin/python scripts/repair_guild_zero.py --home 1492194186101919915
    ./.venv/bin/python scripts/repair_guild_zero.py --home 1492194186101919915 --apply
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from data.store import _db  # noqa: E402

SRC = "0"
SUBCOLLECTIONS = ["corps", "part_catalogs", "ksp_notifications", "ksp_craft_imports"]
# Top-level collections carrying a guild_id field, and the fields to rewrite in each.
FIELD_FIXUPS = {
    "corp_owners":  ["guild_id"],
    "ksp_sessions": ["guild_id", "ksp_guild_id"],
    "contracts":    ["guild_id"],
}


class Plan:
    def __init__(self):
        self.moves = []      # (subcollection, doc_id, data, dest_exists)
        self.fields = []     # (collection, doc_id, {field: (old, new)})
        self.backup = {"moves": {}, "fields": {}, "dest_prior": {}}

    def empty(self):
        return not self.moves and not self.fields


def build_plan(home: str) -> Plan:
    plan = Plan()
    src_doc = _db.collection("guilds").document(SRC)

    for name in SUBCOLLECTIONS:
        for snap in src_doc.collection(name).stream():
            data = snap.to_dict() or {}
            dest = _db.collection("guilds").document(home).collection(name).document(snap.id)
            dest_snap = dest.get()
            plan.moves.append((name, snap.id, data, dest_snap.exists))
            plan.backup["moves"][f"{name}/{snap.id}"] = data
            if dest_snap.exists:
                plan.backup["dest_prior"][f"{name}/{snap.id}"] = dest_snap.to_dict()

    for coll, fields in FIELD_FIXUPS.items():
        try:
            rows = list(_db.collection(coll).stream())
        except Exception as exc:                       # pragma: no cover - defensive
            print(f"  ! could not read {coll}: {exc}")
            continue
        for snap in rows:
            d = snap.to_dict() or {}
            changes = {f: (d.get(f), home) for f in fields if str(d.get(f)) == SRC}
            if changes:
                plan.fields.append((coll, snap.id, changes))
                plan.backup["fields"][f"{coll}/{snap.id}"] = {
                    f: d.get(f) for f in fields
                }
    return plan


def show(plan: Plan, home: str) -> None:
    print(f"\nguilds/{SRC}/* -> guilds/{home}/*")
    if not plan.moves:
        print("    (nothing to move)")
    for name, doc_id, data, dest_exists in plan.moves:
        label = data.get("owner_name") or data.get("name") or ""
        if dest_exists:
            print(f"    SKIP  {name}/{doc_id}  {label!r}  (destination already exists)")
        else:
            print(f"    move  {name}/{doc_id}  {label!r}")

    print(f"\nguild_id field rewrites  {SRC!r} -> {home!r}")
    if not plan.fields:
        print("    (nothing to rewrite)")
    for coll, doc_id, changes in plan.fields:
        flds = ", ".join(f"{f}: {old!r} -> {new!r}" for f, (old, new) in changes.items())
        print(f"    {coll}/{doc_id}   {flds}")


def apply(plan: Plan, home: str) -> None:
    moved = skipped = 0
    for name, doc_id, data, dest_exists in plan.moves:
        src = _db.collection("guilds").document(SRC).collection(name).document(doc_id)
        dest = _db.collection("guilds").document(home).collection(name).document(doc_id)
        if dest_exists:
            # The account already has a record in the home guild. Leaving the guild-0
            # copy in place would be harmless (nothing reads guild 0), but deleting it
            # without having written anything is the one irreversible step here, so it
            # is left for a human to remove once they have looked at both.
            print(f"    SKIP  {name}/{doc_id} (destination exists; source left in place)")
            skipped += 1
            continue
        dest.set(data)
        # Verify the read-back before deleting anything.
        check = dest.get()
        if not check.exists or (check.to_dict() or {}) != data:
            print(f"    !! {name}/{doc_id}: copy did not verify, source left in place")
            continue
        src.delete()
        print(f"    moved {name}/{doc_id}")
        moved += 1

    rewritten = 0
    for coll, doc_id, changes in plan.fields:
        update = {f: new for f, (_old, new) in changes.items()}
        _db.collection(coll).document(doc_id).set(update, merge=True)
        print(f"    set   {coll}/{doc_id} {update}")
        rewritten += 1

    print(f"\n  {moved} moved, {skipped} skipped, {rewritten} field rewrite(s).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True,
                    help="destination guild id (the value now in HOME_GUILD_ID)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args()

    home = str(args.home).strip()
    if not home.isdigit() or home == "0":
        print("--home must be a real guild id (digits, not 0).")
        return 2

    print(f"Repairing records stranded on guild {SRC!r} -> {home!r}")
    print("DRY RUN (pass --apply to write)\n" if not args.apply else "APPLYING\n")

    # Sanity: refuse to move into a guild with no sign of life, which would mean a
    # typo'd id and a second stranding rather than a repair.
    dest_corps = len(list(_db.collection("guilds").document(home)
                          .collection("corps").limit(1).stream()))
    dest_doc = _db.collection("guilds").document(home).get()
    if not dest_corps and not dest_doc.exists:
        print(f"  ! guilds/{home} has no document and no corps. That looks like the "
              f"wrong id.\n    Re-check it against HOME_GUILD_ID before continuing.")
        return 2

    plan = build_plan(home)
    show(plan, home)

    if plan.empty():
        print("\nNothing to repair. (Already run, or nothing was stranded.)")
        return 0

    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"guild_zero_backup_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan.backup, fh, indent=2, default=str)
    print(f"\n  backup written: {path}")

    apply(plan, home)
    print("\nDone. Restart is not required; nothing here is cached beyond "
          "`_recip_guild_cache` (30s TTL) and the corps picker's own read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
