"""Properties that HOLD (controls) — money conservation under concurrency, the
largest-remainder split, timed-reward atomicity, and the request-model bounds.
A BUG line here would be a store-level mint."""
import asyncio, random
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, wallet, bal, ledger_sum, run, store, GID
from api_models import FinanceSendRequest, ContractCreateRequest, WebAuctionBidRequest


async def main():
    section("A. try_debit / add_balance / garnish under 400 interleaved tasks conserve coins")
    ids = [f"u{i}" for i in range(6)]
    for u in ids:
        wallet(u, balance=0)
        await store.add_balance(GID, u, 1000, category=store.TX_ADMIN)   # through the ledger
    store.get_user(GID, "u0")["debts"] = [{"creditor_id": "u1", "amount": 300}, {"creditor_id": "u2", "amount": 7}]
    async def move():
        a, b = random.sample(ids, 2)
        amt = random.randint(1, 400)
        if await store.try_debit(GID, a, amt, category=store.TX_TRANSFER_OUT):
            await asyncio.sleep(0)
            await store.add_balance(GID, b, amt, garnishable=True, category=store.TX_TRANSFER_IN)
    await asyncio.gather(*(move() for _ in range(400)))
    total = sum(bal(u) for u in ids)
    check("total coins unchanged (garnish only moves coins between wallets)", total == 6000, total)
    check("no wallet negative", all(bal(u) >= 0 for u in ids))
    check("every ledger sums to its wallet", all(ledger_sum(u) == bal(u) for u in ids),
          [(u, bal(u), ledger_sum(u)) for u in ids if ledger_sum(u) != bal(u)])
    check("no debt went negative", all(d["amount"] >= 0 for d in store.get_user(GID, "u0")["debts"]))

    section("B. largest-remainder split: pays exactly `take`, never more than owed")
    bad = 0
    for _ in range(3000):
        n = random.randint(1, 6)
        debts = [{"creditor_id": f"c{i}", "amount": random.randint(1, 500)} for i in range(n)]
        owed = {d["creditor_id"]: d["amount"] for d in debts}
        u = wallet("dbt", balance=0, debts=debts)
        for cid in owed: wallet(cid, balance=0)
        gross = random.randint(1, 2000)
        before = sum(owed.values())
        _, paid = await store.add_balance_gross(GID, "dbt", gross, garnishable=True)
        take = sum(a for _, a in paid)
        if (bal("dbt") != gross - take or any(bal(c) != a for c, a in paid)
                or any(a > owed[c] for c, a in paid)
                or take > min(before, gross * store._garnish_percent(before) // 100)):
            bad += 1
    check("3000 random splits: sum(paid) == take, no over-payment, no strand", bad == 0, bad)

    section("C. timed reward: 50 concurrent claims pay once")
    wallet("tr", balance=0)
    res = await asyncio.gather(*(store.try_claim_timed_reward(GID, "tr", "k", 300, 3600) for _ in range(50)))
    check("exactly one grant", sum(1 for g, _ in res if g) == 1 and bal("tr") == 300)

    section("D. request-model coercions are harmless at the boundary")
    check("bool True coerces to 1 (pydantic lax) — harmless, still >0",
          FinanceSendRequest(to_user_id="x", amount=True).amount == 1)
    big = 10**40
    wallet("big", balance=10)
    check("10**40 passes the model but try_debit refuses it",
          ContractCreateRequest(contractor_id="1", mission="abc", payment=big, due_date="2099-01-01").payment == big
          and not await store.try_debit(GID, "big", big))
    for bad_v in (-1, 0, 5.5, "1e3"):
        try:
            WebAuctionBidRequest(amount=bad_v); ok = False
        except Exception:
            ok = True
        check(f"bid amount {bad_v!r} rejected", ok)
    finish()

run(main())
