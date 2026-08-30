"""Two-factor authentication: enrolment, replay, disable, and the sign-in
challenge. Data layer (`data/twofa.py`) plus the website endpoints over it."""
import asyncio
import threading
import time

from _h import check, section, finish, src, between, quiet
from _acct import DB, patch_accounts_db

import api_server
import api_auth
from data import twofa

db = DB()
patch_accounts_db(db)
quiet(api_server)

ACC = "a_victim"
ACCOUNT_DOC = {"account_id": ACC, "firebase_uid": "victim", "username": "victim",
               "display_name": "Victim", "email": "v@x.test"}

# ── the website tier, offline ────────────────────────────────────────────────
from fastapi.testclient import TestClient
SECRET = "s" * 48
api_server._get_api_secret = lambda: SECRET
api_server.verify_session_token = lambda tok, sec: api_auth.verify_session_token(tok, SECRET)
api_server.enforce_not_suspended = lambda *a, **k: None
api_server.accounts.get_account = lambda aid: dict(ACCOUNT_DOC) if str(aid) == ACC else None
api_auth._get_token_version = lambda uid: 0
api_auth._sessions_col = lambda: db.collection("ksp_sessions")
client = TestClient(api_server.app, raise_server_exceptions=False)
TOKEN = api_auth.create_session_token("0", ACC, "Victim", SECRET, aud=api_auth.AUD_WEB)
H = {"Authorization": f"Bearer {TOKEN}"}


def enroll():
    started = twofa.begin_enroll(ACC, "Victim")
    ok, msg, codes = twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    assert ok, msg
    return started["secret"], codes


section("controls: what the module gets right")
secret, codes = enroll()
check("enrolment is not enabled until a real code confirms it (begin alone)",
      (twofa.begin_enroll("a_x", "x") and not twofa.is_enabled("a_x")))
now = time.time()
code = twofa.totp_now(secret, now)
db.cols["account_2fa"].docs[ACC]["last_counter"] = twofa.counter_now(now) - 2
ok1, _ = twofa.verify(ACC, code)
ok2, m2 = twofa.verify(ACC, code)
check("a TOTP code cannot be replayed inside its window", ok1 and not ok2, m2)
ok, _ = twofa.disable(ACC, "000000")
check("2FA cannot be disabled without a working code", not ok and twofa.is_enabled(ACC))
ok, _ = twofa.verify(ACC, codes[0])
ok2, _ = twofa.verify(ACC, codes[0])
check("a recovery code is single-use (sequential)", ok and not ok2)
r = client.post("/api/v1/web/account/2fa/disable", json={"code": "123456"}, headers=H)
check("endpoint: disable with a wrong code is refused", r.status_code == 400, r.text)
r = client.post("/api/v1/web/account/2fa/begin", headers=H)
check("endpoint: begin is refused while 2FA is on (normal read)", r.status_code == 409, r.text)
check("endpoint: 2FA still on after that refusal", twofa.is_enabled(ACC))

section("2FA silently disabled by /2fa/begin during a Firestore read blip")
# `web_2fa_begin` gates on twofa.status(), which fails OPEN (enabled=False) on a
# read error; begin_enroll then set()s the record WITHOUT merge, replacing the
# enabled secret with a fresh unconfirmed one.
db.cols["account_2fa"].fail_reads = 1
r = client.post("/api/v1/web/account/2fa/begin", headers=H)
check("begin is refused while 2FA is on even if the status read fails",
      r.status_code != 200, f"got {r.status_code}: {r.text[:80]}")
check("2FA is still enforced after the call", twofa.is_enabled(ACC),
      f"record now: {db.cols['account_2fa'].docs.get(ACC)}")
check("data layer: begin_enroll refuses to overwrite an enabled record",
      False if not twofa.is_enabled(ACC) else True,
      "begin_enroll() uses set() without merge and never checks `enabled`")

section("sign-in challenge attempt cap is not atomic")
db2 = DB(latency=0.05)          # a slow Firestore round-trip
patch_accounts_db(db2)
secret, codes = enroll()
cid = twofa.create_login_challenge(ACC)
results = []

def guess(i):
    got, msg, _ = twofa.resolve_login_challenge(cid, "000000")
    results.append((got, msg))

ts = [threading.Thread(target=guess, args=(i,)) for i in range(40)]
[t.start() for t in ts]; [t.join() for t in ts]
attempts = db2.cols["twofa_login"].docs.get(cid, {}).get("attempts")
judged = sum(1 for g, m in results if "Too many" not in m)
check("at most 5 guesses are judged per challenge",
      judged <= 5, f"{judged} of 40 concurrent wrong guesses were judged; stored attempts={attempts}")
got, msg, _ = twofa.resolve_login_challenge(cid, twofa.totp_now(secret))
check("the challenge is dead after 40 wrong codes", got is None, f"resolved to {got!r} — {msg}")

section("recovery code single-use is not atomic")
secret, codes = enroll()
db2.cols["account_2fa"].docs[ACC]["last_counter"] = 10**9  # force the recovery path
wins = []
def spend():
    ok, _ = twofa.verify(ACC, codes[0])
    wins.append(ok)
ts = [threading.Thread(target=spend) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
check("one recovery code accepted at most once under concurrency",
      sum(wins) <= 1, f"accepted {sum(wins)} times out of 8 concurrent uses")

section("TOTP brute-force budget (from the code's own constants)")
s = src("api_server.py")
web = between(s, "async def web_auth_totp", "\n@app.")
ksp = between(s, "async def auth_link_totp", "\n@app.")
signin = between(s, "async def web_auth_signin", "\n@app.")
per_ip = 20; window = 300
valid = 2 * twofa.DEFAULT_WINDOW + 1
p = valid / 10 ** twofa.DIGITS
per_day_ip = per_ip * 86400 / window
print(f"  info  /web/auth/totp and /auth/link/totp: {per_ip} guesses / {window}s per IP, "
      f"{valid} valid codes of 1e6 -> P(hit)/day/IP = {per_day_ip * p:.4f}; "
      f"expected ~{1 / (per_day_ip * p):.0f} IP-days per takeover of an account whose 1st factor is held")
check("per-challenge cap is backed by a per-IP limiter on both TOTP endpoints",
      "max_hits=20, window=300.0" in web and "max_hits=20, window=300.0" in ksp)
check("a fresh challenge needs a fresh Firebase token (signin is also rate-limited)",
      "signin:" in signin and "max_hits=20" in signin)
check("a wrong guess costs the challenge, not just the IP (attempts counted on the doc)",
      "attempts" in src("data/twofa.py"))
finish()
