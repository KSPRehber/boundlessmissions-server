"""The link-code 'sweep defense' is an attacker-triggerable denial of linking.

`_note_failed_link_guess` purges EVERY outstanding link code once 40 wrong codes
arrive in 180 s. The per-IP limit is 10/min, so four addresses (or one address
behind a rotating proxy) reach the threshold without ever seeing a 429, and can
repeat it every three minutes — nobody can link a KSP client or the website while
it runs. The sweep it defends against was already infeasible: the global cap of
600/min against a 1,000,000-code space gives a 3-minute code a ~0.2% hit chance.
"""
import types
from _h import check, section, finish
import api_server, settings

purges = []
api_server.purge_all_link_codes = lambda: purges.append(1) or 0

def req(ip):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=ip), headers={})

section("four IPs × 10 wrong codes inside one code lifetime")
throttled = 0
for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"):
    for _ in range(settings.KSP_LINK_RATELIMIT_PER_IP):
        try:
            api_server._guard_link_attempt(req(ip))
        except Exception:
            throttled += 1
            continue
        api_server._note_failed_link_guess(ip)
check("the per-IP limit stops the attempt pattern", throttled > 0,
      f"0 of 40 attempts were throttled")
check("outstanding link codes of every player survive 40 wrong guesses",
      not purges, "purge_all_link_codes() fired — every live /linkcode and panel code is burned")
p = 600 * 3 / 1_000_000
print(f"         -> for comparison, a full-rate sweep under the global cap hits one live code "
      f"with probability ~{p:.2%} per code lifetime")
finish()
