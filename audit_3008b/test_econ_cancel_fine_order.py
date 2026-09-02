"""Issuer withdrawal fine is collected BEFORE the escrow refund lands.

`contract_actions.cancel` (ACTIVE, issuer withdrawing) runs
    debit_up_to(issuer, fine) -> add_debt(issuer -> contractor, shortfall)
    -> _pay_issuer(refund=payment)          # non-garnishable, by design
So an issuer whose spare balance is 0 (everything they had is in the escrow) pays
nothing now: the whole fine becomes a debt, then the escrow comes back to a
wallet that is free to spend it. The contractor is owed money the issuer is
holding, collectable only from *future* earnings the issuer need never have.
"""
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, mk, wallet, bal, run, store, ca, cdb, GID


async def main():
    section("A. issuer with only the escrow withdraws an ACTIVE contract")
    wallet("100", balance=0)           # everything the issuer owns is the 100 escrow
    wallet("200", balance=0)
    mk(status=cdb.ACTIVE, payment=100, fine=40)
    r = await ca.cancel(GID, "c1", actor_id="100", actor_name="Issuer")
    check("cancel succeeded", r.ok, r.message)
    check("the contractor received the withdrawal fine now",
          bal("200") == 40,
          f"contractor got {bal('200')}, issuer holds {bal('100')} and owes "
          f"{store.debt_total(GID, '100')} — the escrow refund (100) was credited AFTER the "
          f"fine was collected, so the fine became debt instead of being paid from it")
    check("the issuer is not left holding refunded coins while owing the fine",
          not (bal("100") >= 40 and store.debt_total(GID, "100") > 0),
          f"issuer balance {bal('100')}, debt {store.debt_total(GID, '100')}")

    section("B. control: the same withdrawal with spare coins pays in full")
    wallet("100", balance=40); wallet("200", balance=0)
    mk(status=cdb.ACTIVE, payment=100, fine=40)
    await ca.cancel(GID, "c1", actor_id="100", actor_name="Issuer")
    check("contractor paid 40, issuer refunded 100, no debt",
          bal("200") == 40 and bal("100") == 100 and store.debt_total(GID, "100") == 0)

    section("C. control: a debt is only repaid from earnings, never from refunds or spends")
    # Seeded directly: with the refund-first fix, A's withdrawal leaves no debt to
    # test against, but the property still matters for a fine larger than the escrow.
    wallet("100", balance=100, debts=[{"creditor_id": "200", "amount": 40}]); wallet("200", balance=0)
    # Issuer spends their balance (a spend is never garnished) …
    assert await store.try_debit(GID, "100", 100)
    # … and a refund of their own later escrow is not garnished either.
    await store.add_balance(GID, "100", 500, category=store.TX_CONTRACT_REFUND)
    check("no garnish on non-earnings: contractor still unpaid after issuer moved 600 coins",
          bal("200") == 0 and store.debt_total(GID, "100") == 40)
    finish()

run(main())
