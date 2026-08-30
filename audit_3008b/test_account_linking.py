"""Account linking & id-shape: can a Discord user hijack a web account (or the
reverse), does the merge guard hold, and can an id impersonate the other space."""
import threading
from _h import check, section, finish, src, between
from _acct import DB, patch_accounts_db, MemStore
from data import accounts as acc

db = DB()
patch_accounts_db(db)
acc.store = MemStore()

section("id-shape: the two namespaces are provably disjoint")
check("a snowflake is a Discord account", acc.is_discord_account("123456789012345678"))
check("a web account id is not a Discord one",
      not acc.is_discord_account(acc.firebase_account_id("abc")))
check("a firebase uid that is all digits still cannot forge a snowflake id",
      acc.firebase_account_id("123") == "a_123" and not acc.is_discord_account("a_123"))
# The only way a web id could BE a snowflake is if a firebase uid were empty and
# the prefix dropped — check the prefix is unconditional.
check("firebase_account_id always prefixes, even for an empty uid",
      acc.firebase_account_id("") == "a_" and not acc.is_discord_account("a_"))

section("linking refuses to merge two accounts that both have history")
DISCORD = "555000111222333444"
WEB = acc.firebase_account_id("googleuid")
db.collection("accounts").document(DISCORD).set(
    {"account_id": DISCORD, "discord_id": DISCORD, "username": "jeb"})
db.collection("accounts").document(WEB).set(
    {"account_id": WEB, "firebase_uid": "googleuid", "username": "web_jeb"})
acc.store.get_user(0, DISCORD)["xp"] = 999
acc.store.get_user(0, WEB)["balance"] = 5000
code, msg, kept = acc.join_accounts(DISCORD, WEB)
check("two active accounts cannot be silently merged", code == acc.JOIN_BOTH_ACTIVE, (code, msg))
check("and no wallet was destroyed", acc.store.users[DISCORD]["xp"] == 999
      and acc.store.users[WEB]["balance"] == 5000)

section("linking a Discord id already owned elsewhere is refused")
# WEB account tries to link DISCORD (which resolves to itself, an active account)
c, m = acc.link_discord(WEB, DISCORD)
check("cannot steal a Discord id that belongs to an active account",
      c == acc.LINK_HAS_DATA, (c, m))

section("a fresh Discord id CAN be linked onto a web account (the intended path)")
FRESH_DID = "777000111222333444"
c, m = acc.link_discord(WEB, FRESH_DID)
check("linking a never-seen Discord id succeeds", c == acc.LINK_OK, (c, m))
check("and it now resolves to the web account",
      acc.account_for_discord(FRESH_DID) == WEB)

section("can a second web account then CLAIM that same Discord id? (takeover check)")
WEB2 = acc.firebase_account_id("attacker")
db.collection("accounts").document(WEB2).set(
    {"account_id": WEB2, "firebase_uid": "attacker", "username": "mallory"})
c2, m2 = acc.link_discord(WEB2, FRESH_DID)
check("a Discord id already linked to a live account cannot be re-linked by another",
      c2 in (acc.LINK_TAKEN, acc.LINK_HAS_DATA, acc.LINK_ALREADY, acc.LINK_CONFLICT)
      and acc.account_for_discord(FRESH_DID) == WEB,
      f"code={c2} msg={m2} -> now resolves to {acc.account_for_discord(FRESH_DID)}")

section("consume_link_challenge is single-use (no double-join replay)")
made = acc.create_link_challenge(WEB2)
code = made[0]
first = acc.consume_link_challenge(code)
second = acc.consume_link_challenge(code)
check("a link challenge cannot be spent twice", first == WEB2 and second is None, (first, second))

section("username claim is race-safe (transaction), reserved names blocked")
db.collection("accounts").document("a_p1").set({"account_id": "a_p1", "username": ""})
db.collection("accounts").document("a_p2").set({"account_id": "a_p2", "username": ""})
wins = []
def claim(aid):
    ok, _ = acc.claim_username(aid, "SharedName")
    wins.append((aid, ok))
ts = [threading.Thread(target=claim, args=(a,)) for a in ("a_p1", "a_p2", "a_p1", "a_p2")]
[t.start() for t in ts]; [t.join() for t in ts]
owner = acc.account_for_username("SharedName")
distinct_winners = {a for a, ok in wins if ok}
check("a contested username resolves to exactly one owner",
      owner in ("a_p1", "a_p2") and len(distinct_winners) == 1,
      f"owner={owner} winners={distinct_winners}")
check("reserved usernames are refused", acc.validate_username("admin") is not None)
check("case-folded reserved names too", acc.validate_username("AdMiN") is not None)
finish()
