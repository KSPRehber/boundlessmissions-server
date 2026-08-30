"""cogs/targets.resolve — can a moderator be steered onto the wrong account?

The resolver documents that an *account id* may be typed into the `username`
field (a moderator copying one out of the console), and that the username lookup
runs first "because a name can never look like a snowflake anyway". That last
claim is what this script tests: `validate_username` allows digits-only names up
to 20 characters, and a Discord snowflake is 17-19 digits."""
import asyncio

from _h import check, section, finish

from data import accounts
from data.store import store
from cogs import targets

VICTIM = "190212345678901234"        # a Discord snowflake (18 digits)
ATTACKER = "a_attackerFirebaseUid0000000"

section("a username may be spelled exactly like another player's Discord id")
check("validate_username accepts an 18-digit name", accounts.validate_username(VICTIM) is None,
      accounts.validate_username(VICTIM))
check("validate_username accepts a 19-digit name", accounts.validate_username("1" * 19) is None)

# ── fakes: no Firestore ──────────────────────────────────────────────────────
USERNAMES = {VICTIM: ATTACKER}                       # attacker claimed the name "190212345678901234"
ACCOUNTS = {ATTACKER: {"username": VICTIM, "display_name": "Totally Jeb"},
            VICTIM: {"username": "realjeb", "display_name": "Jeb"}}
accounts.owner_of_username = lambda name: USERNAMES.get(accounts.normalize_username(name), "")
accounts.get_account = lambda aid: ACCOUNTS.get(str(aid))
accounts.account_for_discord = lambda did: str(did)
store._users[VICTIM] = {"balance": 10, "xp": 0}

class Interaction:
    guild = None
    user = None

async def go(member, username, **kw):
    return await targets.resolve(Interaction(), member, username, **kw)

section("resolution order")
t = asyncio.run(go(None, VICTIM))
check("typing the victim's Discord id into `username` resolves to the victim",
      t.account_id == VICTIM,
      f"resolved to {t.account_id} ({t.label}) — the account that CLAIMED the id-shaped username; "
      f"/givemoney, /setbalance, /fine, /setxp, /contractreset all act on it")

# Control: with nobody squatting the name, the id branch works as documented.
USERNAMES.clear()
t = asyncio.run(go(None, VICTIM))
check("control: with no such username, the id branch finds the victim", t.account_id == VICTIM)

section("the documented refusals hold")
class M:  # a discord.Member stand-in
    id = int(VICTIM)
for label, args, kw in (("both fields filled", (M(), "someone"), {}),
                        ("neither filled on a write command", (None, None), {}),
                        ("neither filled, whitespace username", (None, "   "), {})):
    try:
        asyncio.run(go(*args, **kw)); ok = False
    except targets.TargetError:
        ok = True
    check(f"{label} -> refused", ok)

accounts.owner_of_username = lambda name: None          # a failed read
try:
    asyncio.run(go(None, "jeb")); ok = False
except targets.TargetError as e:
    ok = "reach" in str(e)
check("a failed username read is refused as 'try again', not as 'no such player'", ok)

accounts.owner_of_username = lambda name: ""
try:
    asyncio.run(go(None, "999999999999999999")); ok = False
except targets.TargetError as e:
    ok = "No Boundless account" in str(e)
check("an id nobody has a record for is refused (no wallet minted)",
      ok and "999999999999999999" not in store._users)

accounts.account_for_discord = lambda did: None        # index read failed
try:
    asyncio.run(go(M(), None)); ok = False
except targets.TargetError:
    ok = True
check("member path with a failed index read is refused (never falls back to member.id)", ok)

del store._users[VICTIM]
finish()
