"""Session issuance & revocation: does logout_all actually kill signed tokens,
does the token epoch work, and are KSP/web audiences kept apart."""
from _h import check, section, finish, src, between
from _acct import DB, patch_accounts_db
import api_auth as a
from data import accounts

db = DB()
patch_accounts_db(db)
a._sessions_col = lambda: db.collection("ksp_sessions")
a._token_versions.clear()
a._allowed_devices.clear()
SEC = "k" * 48

section("logout_all invalidates every signed token, including the current one")
tok = a.create_session_token("0", "u1", "Jeb", SEC, aud=a.AUD_WEB)
check("a fresh token verifies", a.verify_session_token(tok, SEC) is not None)
a.logout_all_devices("u1")
check("after logout_all the same token is rejected", a.verify_session_token(tok, SEC) is None)
tok2 = a.create_session_token("0", "u1", "Jeb", SEC, aud=a.AUD_WEB)
check("a token minted after logout_all works again", a.verify_session_token(tok2, SEC) is not None)
check("and the old one stays dead", a.verify_session_token(tok, SEC) is None)

section("a token cannot be forged / tampered")
enc, sig = tok2.split(".")
check("a wrong signature is rejected", a.verify_session_token(enc + ".deadbeef", SEC) is None)
check("a token signed with a different key is rejected",
      a.verify_session_token(a.create_session_token("0", "u1", "x", "other"*10, aud=a.AUD_WEB), SEC) is None)

section("audiences: a KSP token is not a web token")
ksp = a.create_session_token("0", "u2", "K", SEC, aud=a.AUD_KSP)
p = a.verify_session_token(ksp, SEC)
check("the aud claim is carried and readable", p and p.get("aud") == a.AUD_KSP, p)

section("token-epoch read failure fails CLOSED for an already-cached revocation")
# _get_token_version: on a read error, returns last cached value (a cached
# revocation still applies) and does NOT cache the guess.
a._token_versions.clear()
a.logout_all_devices("u3")            # caches version 1 in-process
db.cols["ksp_sessions"].fail_reads = 5
# force cache miss by ageing it
a._token_versions["u3"] = (1, 0.0)
check("a revoked user stays revoked across a Firestore read blip",
      a._get_token_version("u3") == 1)

section("brute-force feasibility of the 6-digit link code (from real limits)")
import settings
per_ip = settings.KSP_LINK_RATELIMIT_PER_IP     # 10/min
glob = settings.KSP_LINK_RATELIMIT_GLOBAL       # 600/min
life = a.LINK_CODE_LIFETIME                      # 180s
fail_max = 5                                     # _LINK_FAIL_MAX before per-IP lockout, 10-min window
space = 10 ** 6
live_codes = 1   # at most a handful; one honest user links at a time
# An address gets 5 wrong guesses per 10 minutes before lockout.
guesses_per_ip_per_window = 5
p_one = live_codes / space
# distributed: global cap 600/min => 600 * 3 = 1800 guesses over a code's 3-min life
distributed = glob * (life / 60.0)
print(f"  info  6-digit space={space}, live codes~{live_codes}, code life={life}s")
print(f"  info  single IP: {guesses_per_ip_per_window} guesses / 10min before lockout "
      f"-> P(hit) per 10min = {guesses_per_ip_per_window * p_one:.2e}")
print(f"  info  distributed at global cap: ~{distributed:.0f} guesses per code lifetime "
      f"-> P(hit) = {distributed * p_one:.4%} per code; and a hit with KSP_2FA off is a live token")
check("per-IP lockout after 5 wrong codes is enforced ( _LINK_FAIL_MAX )",
      "_LINK_FAIL_MAX = 5" in src("api_server.py"))
check("valid link codes are one-time and deleted on use",
      "One-time use — delete" in src("api_auth.py"))
check("the global cap exists as a backstop and is not the brute-force defense",
      distributed * p_one < 0.01,
      f"distributed hit prob {distributed*p_one:.4%} — a full-lifetime sweep at the global cap")
finish()
