"""The one that matters most from the non-atomic verify(): a single valid TOTP
code, presented concurrently, passes more than once — defeating the anti-replay
`last_counter` is there to provide."""
import threading, time
from _h import check, section, finish
from _acct import DB, patch_accounts_db
from data import twofa

db = DB(latency=0.05)   # a realistic Firestore round-trip
patch_accounts_db(db)
ACC = "a_v"
started = twofa.begin_enroll(ACC, "V")
secret = started["secret"]
twofa.confirm_enroll(ACC, twofa.totp_now(secret))

section("a valid TOTP code, submitted concurrently, is accepted more than once")
db.cols["account_2fa"].docs[ACC]["last_counter"] = twofa.counter_now() - 2
code = twofa.totp_now(secret)
oks = []
def submit():
    ok, _ = twofa.verify(ACC, code)
    oks.append(ok)
ts = [threading.Thread(target=submit) for _ in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
check("the same code passes at most once (last_counter is TOCTOU-safe)",
      sum(oks) <= 1, f"one code accepted {sum(oks)}/10 times concurrently")
finish()
