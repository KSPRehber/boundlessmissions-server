"""Guard: the test suite must not write to the live database.

Snapshots every document reachable from the root, runs the other suites, and
diffs. Slower than the rest (it walks the whole project), so it is not part of a
normal run — invoke it after touching anything a test exercises.

Written after a first attempt at this missed a leak: it enumerated only top-level
collections, and `guilds/{id}/corps` is a subcollection, so a corp record written
by a test walked straight past the check and into the live player picker.
`collections()` recursion is the fix — it finds subcollections rather than
assuming a list of names is complete.
"""
import os
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data.store import _db


def walk(ref=None, depth=0):
    out = set()
    cols = _db.collections() if ref is None else ref.collections()
    for col in cols:
        for doc in col.stream():
            out.add(doc.reference.path)
            if depth < 2:                       # deep enough for guilds/*/corps
                out |= walk(doc.reference, depth + 1)
    return out


before = walk()
print(f"before: {len(before)} documents")

for t in ("test_web_accounts.py", "test_account_id_shapes.py", "test_tickets.py",
          "test_accounts.py", "test_twofa.py", "test_rewards.py",
          "test_contract_actions.py"):
    r = subprocess.run([sys.executable, t], capture_output=True, text=True,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    print(f"  {'PASS' if r.returncode == 0 else 'FAIL'}  {t}")

after = walk()
print(f"after : {len(after)} documents")

created = sorted(after - before)
removed = sorted(before - after)
print("\ncreated by the run:", created or "NONE")
print("removed by the run:", removed or "NONE")
sys.exit(1 if (created or removed) else 0)
