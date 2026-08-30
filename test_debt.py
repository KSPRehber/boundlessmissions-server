"""
test_debt.py — the fine-debt ledger and earnings garnishment in `data/store.py`.

Runs against the real `store` singleton but never flushes: every assertion is on the
in-memory record, and nothing here calls `save_if_dirty`, so no Firestore write is
made. `_mark_dirty` only adds an id to a set.

What is worth testing here is the arithmetic, because it is the part that cannot be
read off the call sites: the pro-rata split has to divide an integer wallet without
losing or inventing coins, and the opt-in has to keep refunds out.
"""

import asyncio
import sys

import settings
from data.store import store

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


DEBTOR, A, B, C = 9001, 9002, 9003, 9004


def reset(balance=0):
    for uid in (DEBTOR, A, B, C):
        u = store.get_user(0, uid)
        u["balance"] = 0
        u["debts"] = []
    store.get_user(0, DEBTOR)["balance"] = balance


async def main():
    print("debt ledger")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 100)
    await store.add_debt(0, DEBTOR, str(A), 50)
    check("repeat debts to one creditor merge",
          store.list_debts(0, DEBTOR) == [{"creditor_id": str(A), "amount": 150}],
          store.list_debts(0, DEBTOR))
    await store.add_debt(0, DEBTOR, str(B), 25)
    check("a second creditor is a second entry", store.debt_total(0, DEBTOR) == 175)

    print("\nonly flagged credits are garnished")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 100)
    await store.add_balance(0, DEBTOR, 40)
    check("an unflagged credit is left alone",
          store.get_user(0, DEBTOR)["balance"] == 40 and store.debt_total(0, DEBTOR) == 100,
          (store.get_user(0, DEBTOR)["balance"], store.debt_total(0, DEBTOR)))
    check("and pays the creditor nothing", store.get_user(0, A)["balance"] == 0)

    print("\nearnings are garnished at the base rate")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 100)
    bal, paid = await store.add_balance_gross(0, DEBTOR, 40, garnishable=True)
    want = 40 * settings.DEBT_GARNISH_PERCENT // 100
    check(f"{settings.DEBT_GARNISH_PERCENT}% of the credit is taken", bal == 40 - want, bal)
    check("the creditor is paid it", store.get_user(0, A)["balance"] == want)
    check("the debt drops by it", store.debt_total(0, DEBTOR) == 100 - want)
    check("and the caller is told what was taken", paid == [(str(A), want)], paid)

    print("\nnothing is taken beyond what is owed")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 6)
    await store.add_balance(0, DEBTOR, 400, garnishable=True)
    check("the skim stops at the debt", store.get_user(0, A)["balance"] == 6,
          store.get_user(0, A)["balance"])
    check("the ledger is empty", store.debt_total(0, DEBTOR) == 0)
    check("and the earner keeps the rest", store.get_user(0, DEBTOR)["balance"] == 394,
          store.get_user(0, DEBTOR)["balance"])

    print("\nthe split is pro-rata and loses no coins")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 600)
    await store.add_debt(0, DEBTOR, str(B), 300)
    await store.add_debt(0, DEBTOR, str(C), 100)
    _bal, paid = await store.add_balance_gross(0, DEBTOR, 101, garnishable=True)
    take = 101 * settings.DEBT_GARNISH_PERCENT // 100
    check("every coin taken reaches a creditor", sum(a for _c, a in paid) == take, (paid, take))
    check("the debtor lost exactly that", store.get_user(0, DEBTOR)["balance"] == 101 - take)
    got = {c: a for c, a in paid}
    check("shares follow the ratio owed",
          got[str(A)] >= got[str(B)] >= got[str(C)] and got[str(A)] == 30, got)
    check("the ledger fell by the same amount",
          store.debt_total(0, DEBTOR) == 1000 - take, store.debt_total(0, DEBTOR))

    print("\nthe rate rises with the amount owed, not the creditor count")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 2)
    await store.add_debt(0, DEBTOR, str(B), 2)
    await store.add_debt(0, DEBTOR, str(C), 2)
    check("three small debts stay at the base rate",
          store.garnish_percent(0, DEBTOR) == settings.DEBT_GARNISH_PERCENT)
    reset()
    await store.add_debt(0, DEBTOR, str(A), settings.DEBT_GARNISH_ESCALATE_AT)
    check("one large debt escalates",
          store.garnish_percent(0, DEBTOR) == settings.DEBT_GARNISH_PERCENT_MAX)
    check("owing nothing garnishes nothing", store.garnish_percent(0, B) == 0)

    print("\ndust is forgiven rather than left to garnish forever")
    reset()
    await store.add_debt(0, DEBTOR, str(A), settings.DEBT_FORGIVE_BELOW + 1)
    await store.add_balance(0, DEBTOR, 4, garnishable=True)
    check("a debt below the floor is written off", store.debt_total(0, DEBTOR) == 0,
          store.list_debts(0, DEBTOR))

    print("\na bot-issued fine is collected but paid to nobody")
    reset()
    await store.add_debt(0, DEBTOR, "", 100)
    _bal, paid = await store.add_balance_gross(0, DEBTOR, 40, garnishable=True)
    check("it still leaves the debtor", paid == [("", 20)], paid)
    check("the debt still falls", store.debt_total(0, DEBTOR) == 80)

    print("\nthe skim never drives a balance negative")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 10_000)
    store.get_user(0, DEBTOR)["balance"] = 0
    await store.add_balance(0, DEBTOR, 3, garnishable=True)
    check("balance stays at or above zero", store.get_user(0, DEBTOR)["balance"] >= 0,
          store.get_user(0, DEBTOR)["balance"])

    print("\nclear_debts writes the whole ledger off")
    reset()
    await store.add_debt(0, DEBTOR, str(A), 70)
    wiped = await store.clear_debts(0, DEBTOR)
    check("it reports what it wiped", wiped == 70, wiped)
    check("and the ledger is empty", store.debt_total(0, DEBTOR) == 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
