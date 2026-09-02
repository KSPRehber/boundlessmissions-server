#!/usr/bin/env python3
"""
tools/sweep_id_shaped_usernames.py – find (and optionally release) username
reservations that are shaped like account ids.

`data/accounts.validate_username` now refuses a username that is all digits or
starts with `a_`, because `cogs/targets.resolve` accepts an account id in the
same field a username goes in — so a username spelled as another player's
Discord snowflake used to redirect every moderator command aimed at that player
onto whoever claimed it. The rule stops new claims; this sweep finds the ones
made before it existed.

Read-only by default: it prints every `usernames/{name}` reservation whose id
`looks_like_account_id`, together with the account that owns it. With `--apply`
it deletes the reservation and clears `username` on the owning account, which
sends that player back through onboarding to pick a real name (usernames are
otherwise permanent, so this is the one deliberate exception, and the reason the
default is to look and not touch).

    cd "GK Discord Bot" && .venv/bin/python tools/sweep_id_shaped_usernames.py
    cd "GK Discord Bot" && .venv/bin/python tools/sweep_id_shaped_usernames.py --apply

Needs the bot's `.env` (FIREBASE_CREDENTIALS) like any other script that imports
`data.store`. Every read here is metered by the cost guard like the bot's own.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firebase_admin import firestore  # noqa: E402

from data import accounts  # noqa: E402


def main(apply: bool) -> int:
    hits = []
    for snap in accounts._usernames().stream():
        if accounts.looks_like_account_id(snap.id):
            owner = str((snap.to_dict() or {}).get("account_id") or "")
            hits.append((snap.id, owner))

    if not hits:
        print("No id-shaped username reservations found.")
        return 0

    for name, owner in hits:
        print(f"  usernames/{name}  ->  account {owner or '(no owner recorded)'}")
    print(f"{len(hits)} id-shaped reservation(s).")

    if not apply:
        print("Dry run. Re-run with --apply to release them.")
        return 1

    for name, owner in hits:
        accounts._usernames().document(name).delete()
        if owner:
            # Clear rather than rename: there is no name to give them, and an
            # empty field is exactly the state onboarding already handles.
            accounts._accounts().document(owner).set(
                {"username": firestore.DELETE_FIELD}, merge=True)
        print(f"  released usernames/{name} (account {owner or '-'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
