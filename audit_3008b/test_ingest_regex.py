"""ReDoS / super-linear regex on mission text, and where it runs synchronously.

`mission_constraints.extract_heuristic` is super-linear in a run of digits
(~n^2.3: 2.5k → 0.6 s, 5k → 2.2 s, 10k → 11 s) and `int()`s any digit run it
matches, which raises ValueError past Python's 4300-digit limit. Mission text is
capped at 500 chars on every path except /contracts/create_rescue (uncapped, up to
starlette's 1 MiB form-field limit), whose contracts store no `constraints` — so
`contract_views._embed` (line 196: `c.get("constraints") or mc.extract_heuristic(...)`)
re-derives them from the mission text every time the contract is rendered for
Discord, synchronously on the bot's event loop.
"""
import time
from _h import check, section, finish, quiet, src, between
import api_server
from data import mission_constraints as mc, orbit_constraints as oc
quiet(api_server)

def timed(fn, text):
    t0 = time.perf_counter()
    try:
        fn(text); err = None
    except Exception as exc:
        err = exc
    return time.perf_counter() - t0, err

adversarial = {
    "digits":        "1" * 5000,
    "digits-space":  ("1 " * 2500),
    "crew of 1..":   "crew of " + "1" * 4990,
    "between":       "between " + "1 and " * 800,
    "CapsWords":     ("Aaaa " * 1000),
    "quotes":        '"' + "a" * 4998 + '"',
    "quote-runs":    '"a ' * 1600,
    "dv":            "at least " + "1" * 4000 + " m/s",
    "km":            ("100 km " * 700),
    "orbit":         ("polar orbit around Kerbin at " + "9" * 100 + " km ") * 40,
    "kerbal-mix":    ("2 kerbals or more " * 250),
    "engine":        ("LV-N Nerv engine " * 290),
    "backtrack":     ("a " * 2000 + "b"),
}
for fn_name, fn in (("mc.extract_heuristic", mc.extract_heuristic),
                    ("oc.extract_heuristic", oc.extract_heuristic),
                    ("_classify_text_heuristic", api_server._classify_text_heuristic)):
    section(fn_name)
    worst = {500: 0.0, 5000: 0.0}; errors = []
    for label, text in adversarial.items():
        for n in (500, 5000):
            dt, err = timed(fn, text[:n])
            worst[n] = max(worst[n], dt)
            if err: errors.append((label, n, err))
            if dt > 0.05:
                print(f"         -> {label!r:16} n={n:5d}: {dt*1000:7.1f} ms")
    check(f"{fn_name}: 500-char worst case < 50 ms", worst[500] < 0.05, f"{worst[500]*1000:.0f} ms")
    check(f"{fn_name}: 5000-char worst case < 500 ms", worst[5000] < 0.5,
          f"{worst[5000]*1000:.0f} ms (reachable only through the uncapped rescue mission)")
    check(f"{fn_name}: never raises on hostile text", not errors,
          "; ".join(f"{l!r} n={n}: {type(e).__name__}: {str(e)[:70]}" for l, n, e in errors))

section("growth and the synchronous render path")
ts = []
for n in (2500, 5000, 10000):
    dt, _ = timed(mc.extract_heuristic, "1" * n); ts.append(dt)
    print(f"         -> {n:5d} digits: {dt:.2f}s")
import math
exp = math.log(ts[2] / ts[0]) / math.log(4)
check("mc.extract_heuristic is ~linear in mission length", exp < 1.3,
      f"~n^{exp:.1f}; a 100 KB rescue mission (allowed: no cap, 1 MiB form-field limit) extrapolates to "
      f"~{ts[2] * (100_000/10_000) ** exp / 60:.0f} minutes per render")
cv = src("cogs/contract_views.py")
check("_embed does not re-derive constraints from the mission text on the event loop",
      'c.get("constraints") or mc.extract_heuristic' not in cv,
      "contract_views.py:196 — a rescue contract stores no constraints, so every _embed() (offer delivery, "
      "dispute, sue ticket, review) runs the heuristic over the raw mission text in the bot's event loop; "
      "asyncio makes that a stall of the whole process (Discord gateway + every API request)")
api = src("api_server.py")
rescue = between(api, "async def create_rescue_contract(", "\n@app.")
check("create_rescue_contract stores constraints (so nothing re-derives them later)",
      "constraints=" in between(rescue, "c = cdb.create_contract(", ")"))
finish()
