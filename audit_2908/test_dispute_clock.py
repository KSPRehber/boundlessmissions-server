"""The dispute clock and the more-time allowance.

`expire_dispute` fines a disputed contract DISPUTE_AUTO_FINE_DAYS after
`disputed_at`. It does not look at `pending_request`: a contractor who asked for
more time (or to settle) and whose issuer simply never answered is fined for the
issuer's silence, exactly as if they had done nothing. And `expire_overdue`
writes DISPUTED by hand rather than through `open_dispute_fields`, so the
per-dispute more-time allowance that the docstring promises is not restored for
the one path that most needs it.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


from _harness import check, section, finish, quiet
import settings
import api_server
import contract_actions as ca
from data import contracts as cdb
from data.store import store

api_server._bot_user_id = 777
quiet(api_server)

CONTRACTS: dict[str, dict] = {}
cdb.get_contract = lambda gid, cid: copy.deepcopy(CONTRACTS.get(cid))
cdb.update_contract = lambda gid, cid, **f: CONTRACTS[cid].update(f)


async def _no_dm(*a, **k):
    return False


ca._dm_more_time_request = _no_dm
ca._dm_settle_request = _no_dm
ca._dm_dispute_options = _no_dm


def reset(uid, balance):
    u = store.get_user(0, uid)
    u.update({"balance": balance, "xp": 0, "debts": [], "tx": [], "tx_totals": {}})


def base(status, **kw):
    c = {"contract_id": "d1", "guild_id": "0", "issuer_id": "9401", "issuer_name": "Boss",
         "contractor_id": "9402", "contractor_name": "Worker", "mission": "Land on Mun",
         "payment": 500, "fine": 200, "due_date": "2099-01-01", "status": status,
         "pending_request": None, "more_time_requests": 0}
    c.update(kw)
    return c


async def main():
    days = settings.DISPUTE_AUTO_FINE_DAYS
    old = (utcnow() - timedelta(days=days + 1)).isoformat()

    section("control: a fresh dispute is not fined")
    reset("9401", 0)
    reset("9402", 1000)
    CONTRACTS["d1"] = base(cdb.DISPUTED, disputed_at=utcnow().isoformat())
    r = await ca.expire_dispute(0, "d1")
    check("expire_dispute refuses before the deadline", not r.ok and r.code == ca.BAD_STATE)

    section("dispute clock vs an unanswered extension request")
    reset("9401", 0)
    reset("9402", 1000)
    CONTRACTS["d1"] = base(cdb.DISPUTED, disputed_at=old, pending_request={
        "kind": ca.REQUEST_MORE_TIME, "new_date": "2099-06-01",
        "requested_at": old, "requested_by": "9402"})
    r = await ca.expire_dispute(0, "d1")
    fined = 1000 - store.get_user(0, "9402")["balance"]
    check("a contractor whose extension request the issuer never answered is not fined "
          "as if they had done nothing", not r.ok or fined == 0,
          f"status={CONTRACTS['d1']['status']} fined={fined} (fine 200): the issuer's "
          f"silence is the contractor's penalty; the request is silently cleared")

    section("dispute clock vs an unanswered settlement request")
    reset("9401", 0)
    reset("9402", 1000)
    CONTRACTS["d1"] = base(cdb.DISPUTED, disputed_at=old, pending_request={
        "kind": ca.REQUEST_SETTLE, "new_date": None,
        "requested_at": old, "requested_by": "9402"})
    r = await ca.expire_dispute(0, "d1")
    fined = 1000 - store.get_user(0, "9402")["balance"]
    check("same for an unanswered settle request", not r.ok or fined == 0,
          f"status={CONTRACTS['d1']['status']} fined={fined}")

    section("more-time allowance after going overdue")
    reset("9401", 0)
    reset("9402", 1000)
    overdue = (utcnow() - timedelta(days=settings.CONTRACT_OVERDUE_GRACE_DAYS + 3)
               ).strftime("%Y-%m-%d")
    # The contractor used their one extension in an earlier dispute, got it, then
    # missed the new deadline: the docstring of open_dispute_fields says a fresh
    # dispute restores the allowance.
    CONTRACTS["d1"] = base(cdb.ACTIVE, due_date=overdue, more_time_requests=1)
    r = await ca.expire_overdue(0, "d1")
    check("control: an overdue ACTIVE contract goes to dispute uncharged",
          r.ok and CONTRACTS["d1"]["status"] == cdb.DISPUTED
          and store.get_user(0, "9402")["balance"] == 1000)
    check("the fresh dispute opened by expire_overdue carries a fresh more-time allowance",
          int(CONTRACTS["d1"].get("more_time_requests") or 0) == 0,
          f"more_time_requests={CONTRACTS['d1'].get('more_time_requests')} — "
          f"expire_overdue writes DISPUTED by hand instead of via open_dispute_fields()")
    r = await ca.dispute(0, "d1", actor_id="9402", actor_name="Worker", action="more_time",
                         new_date="2099-06-01")
    check("… so the contractor can ask for more time on it", r.ok,
          f"{r.code}: {r.message}")

    section("consistency: every writer of DISPUTED uses open_dispute_fields")
    import inspect
    src_ca = inspect.getsource(ca)
    writes = src_ca.count("status=cdb.DISPUTED")
    check("contract_actions never hand-writes status=cdb.DISPUTED", writes == 0,
          f"{writes} hand-written DISPUTED write(s) (expire_overdue)")
    finish()


asyncio.run(main())
