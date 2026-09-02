"""Enumeration, auth coverage on account mutations, and the escalation that the
/2fa/begin overwrite enables (attacker re-owns a victim's second factor)."""
from _h import check, section, finish, src, between
from _acct import DB, patch_accounts_db
from data import twofa

s = src("api_server.py")

section("every account-mutation endpoint sits behind an authenticated dependency")
for name in ("web_account_claim_username", "web_account_display_name",
             "web_account_avatar", "web_account_discord_code", "web_2fa_begin",
             "web_2fa_confirm", "web_2fa_disable", "web_2fa_recovery",
             "web_account_ksp_code", "web_account_ksp_approve"):
    body = between(s, f"async def {name}", "\n@app.")
    sig = body.split("):")[0]
    check(f"{name} requires get_account_user/get_web_user",
          "get_account_user" in sig or "get_web_user" in sig, sig.strip()[:100])

section("username enumeration is bounded, not free")
u = between(s, "async def web_account_claim_username", "\n@app.")
check("the claim endpoint is authenticated (no anonymous probing)",
      "get_account_user" in u.split("):")[0])
check("and rate-limited to make sweeping expensive",
      "_rate_limit(f\"uname:" in u)
check("there is no unauthenticated 'is this username taken' endpoint",
      "owner_of_username" not in s and "account_for_username" not in s,
      "a public username-existence endpoint would enumerate the whole userbase")

section("the disable/recovery gates require a working code (control)")
db = DB(); patch_accounts_db(db)
ACC = "a_x"
st = twofa.begin_enroll(ACC, "X")
twofa.confirm_enroll(ACC, twofa.totp_now(st["secret"]))
ok, _ = twofa.disable(ACC, "000000")
check("disable is refused without a code", not ok and twofa.is_enabled(ACC))
ok, _, _ = twofa.regenerate_recovery_codes(ACC, "000000")
check("recovery regen is refused without a code", not ok)

section("ESCALATION: begin_enroll overwrites an ENABLED factor with no code")
# The data-layer primitive behind the endpoint finding: begin_enroll uses
# set() (no merge) and never checks `enabled`, so calling it on a live 2FA
# record silently downgrades it to a fresh unconfirmed secret the CALLER chose.
before = twofa.is_enabled(ACC)
attacker = twofa.begin_enroll(ACC, "X")          # what /2fa/begin runs on the record
mid = twofa.is_enabled(ACC)
# attacker then confirms THEIR secret -> they own the victim's second factor
ok = False
if attacker is not None:                          # None = refused (the fix)
    ok, _, codes = twofa.confirm_enroll(ACC, twofa.totp_now(attacker["secret"]))
check("begin_enroll must NOT silently disable an already-enabled factor",
      before and mid, f"before={before} after_begin={mid} (record was overwritten with enabled=False)")
check("a chosen secret must NOT become the account's factor without an existing code",
      not (ok and twofa.is_enabled(ACC)),
      "attacker's authenticator now gates the victim account, with fresh recovery codes")
check("the /2fa/begin gate should fail CLOSED, but status() fails open to enabled=False",
      "twofa.status" not in between(s, "async def web_2fa_begin", "\n@app.")
      and "twofa.is_enabled" in between(s, "async def web_2fa_begin", "\n@app."),
      "is_enabled fails closed; status() (what the gate reads) returns enabled=False on a read error")
finish()
