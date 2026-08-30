"""A bot-issued contract's fine can be stalled forever.

`dispute(more_time)` on a bot contract self-extends to end-of-week and sets it
ACTIVE; when that passes, `expire_overdue` pushes it back into DISPUTED through
`open_dispute_fields`, which RESETS `more_time_requests` to 0 — so the one
extension per dispute becomes one per week, for as long as the contractor
keeps pressing the button. `expire_dispute` (the only thing that charges the
fine) is never reached.
"""
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, mk, wallet, bal, run, store, ca, cdb, GID, BOT, settings


async def main():
    section("A. bot contract: more_time -> overdue -> more_time -> … never fined")
    wallet("200", balance=1000)
    c = mk(status=cdb.DISPUTED, issuer=BOT, contractor="200", payment=100, fine=50,
           disputed_at="2000-01-01T00:00:00")
    ca._end_of_week = lambda: "2000-01-08"        # the week that was granted is long gone
    loops = 0
    for _ in range(10):
        r = await ca.dispute(GID, "c1", actor_id="200", actor_name="C", action="more_time")
        if not r.ok:
            break
        r2 = await ca.expire_overdue(GID, "c1")
        if not r2.ok:
            break
        loops += 1
    check("a bot contract cannot cycle more_time/overdue indefinitely", loops < 3,
          f"{loops} more_time→overdue cycles in a row; more_time_requests is reset to 0 by "
          f"open_dispute_fields on every overdue, so DISPUTE_MAX_MORE_TIME_REQUESTS="
          f"{settings.DISPUTE_MAX_MORE_TIME_REQUESTS} never binds")
    check("the fine was never charged in the meantime", bal("200") == 1000)

    section("B. control: a human contract's more_time is a request and is capped per dispute")
    mk(status=cdb.DISPUTED, issuer="100", contractor="200", payment=100, fine=50,
       disputed_at="2000-01-01T00:00:00")
    r = await ca.dispute(GID, "c1", actor_id="200", actor_name="C", action="more_time", new_date="2099-01-01")
    r2 = await ca.dispute(GID, "c1", actor_id="200", actor_name="C", action="more_time", new_date="2099-01-02")
    check("second request in one dispute refused", r.ok and not r2.ok)
    finish()

run(main())
