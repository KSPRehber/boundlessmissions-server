"""
test_finance.py — the transaction ledger in `data/store.py`.

Runs against the real `store` singleton but never flushes: every assertion is on the
in-memory record, and nothing here calls `save_if_dirty`, so no Firestore write is
made (see test_debt.py, which this mirrors).

What is worth testing here is the part that cannot be read off a call site:

  • the ledger must **reconcile** — the entries have to sum to the balance they
    claim to explain, which is the only thing that makes the panel worth showing;
  • the **clamp**, where `add_balance` moves less than it was asked to and a ledger
    recording the request rather than the delta would drift out of that agreement;
  • the split between the **ring buffer** and the **lifetime totals**, which exists
    precisely so that a summary does not shrink as old entries roll off the end;
  • and the fact that garnishment writes **two** entries, one per side, since the
    creditor is not the party making the call and nothing else would explain the
    money arriving in their wallet.

Plus `api_server._escrow_held`, which is not part of the ledger at all: an escrow
that ends by paying the contractor is recorded on the *contractor's* ledger, so the
issuer's own history cannot say how much of what they escrowed is still locked. It
is derived from the contracts and the open auctions instead, and what is worth
testing is exactly the set it counts.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")

from data import store as store_mod
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


PAYER, PAYEE, DEBTOR, CREDITOR = 9101, 9102, 9103, 9104


def reset(balance=0):
    for uid in (PAYER, PAYEE, DEBTOR, CREDITOR):
        u = store.get_user(0, uid)
        u["balance"] = 0
        u["debts"] = []
        u["tx"] = []
        u["tx_totals"] = {}
    store.get_user(0, PAYER)["balance"] = balance


def ledger(uid):
    return store.get_user(0, uid)["tx"]


async def main():
    print("every movement is recorded, and the entries reconcile with the balance")
    reset()
    await store.add_balance(0, PAYER, 1000, category=store.TX_REWARD, detail="Signup")
    await store.try_debit(0, PAYER, 250, category=store.TX_MARKET_PURCHASE,
                          detail="Bought 'Kerbal X'", counterparty=str(PAYEE))
    bal = store.get_user(0, PAYER)["balance"]
    check("a credit and a debit both land", len(ledger(PAYER)) == 2, ledger(PAYER))
    check("the entries sum to the balance", sum(e["a"] for e in ledger(PAYER)) == bal, bal)
    check("the debit is signed negative", ledger(PAYER)[1]["a"] == -250, ledger(PAYER)[1])
    check("the counterparty is kept", ledger(PAYER)[1]["p"] == str(PAYEE))

    print("\nthe recorded amount is what MOVED, not what was asked for")
    # Credited through add_balance rather than assigned by `reset`, so the opening
    # 100 is itself in the ledger — the reconciliation check below is only meaningful
    # if every coin in the wallet got there through a recorded movement.
    reset()
    await store.add_balance(0, PAYER, 100, category=store.TX_REWARD, detail="Opening")
    await store.add_balance(0, PAYER, -5000, category=store.TX_ADMIN, detail="Wipe")
    check("the balance clamps at zero", store.get_user(0, PAYER)["balance"] == 0)
    check("and the entry records the clamped delta", ledger(PAYER)[-1]["a"] == -100,
          ledger(PAYER)[-1])
    check("so the ledger still reconciles", sum(e["a"] for e in ledger(PAYER)) == 0)

    reset()
    await store.add_balance(0, PAYER, 30, category=store.TX_REWARD)
    took = await store.debit_up_to(0, PAYER, 100, category=store.TX_CONTRACT_FINE)
    check("debit_up_to records only what it took",
          took == 30 and ledger(PAYER)[-1]["a"] == -30, (took, ledger(PAYER)))

    print("\na zero-value movement is not recorded")
    reset(50)
    n = len(ledger(PAYER))
    await store.add_balance(0, PAYER, 0, category=store.TX_ADMIN)
    await store.try_debit(0, PAYER, 0, category=store.TX_ADMIN)
    check("it would only push a real entry off the end", len(ledger(PAYER)) == n)

    print("\nthe list is capped but the totals are not")
    reset()
    await store.add_balance(0, PAYER, 10, category=store.TX_REWARD, detail="early")
    for i in range(store.TX_MAX + 25):
        await store.add_balance(0, PAYER, 1, category=store.TX_MARKET_SALE,
                                detail=f"sale {i}")
    check("the ring buffer holds its cap", len(ledger(PAYER)) == store.TX_MAX,
          len(ledger(PAYER)))
    check("the oldest entry has rolled off the list",
          not any(e["c"] == store.TX_REWARD for e in ledger(PAYER)))
    totals = store.transaction_totals(0, PAYER)
    check("but the totals still remember it",
          totals[store.TX_REWARD]["in"] == 10, totals.get(store.TX_REWARD))
    check("and keep a lifetime count past the cap",
          totals[store.TX_MARKET_SALE]["n"] == store.TX_MAX + 25,
          totals.get(store.TX_MARKET_SALE))

    print("\nreads are newest-first, filterable, pageable and copies")
    newest = store.list_transactions(0, PAYER, limit=3)
    check("newest first", newest[0]["d"] == f"sale {store.TX_MAX + 24}", newest[0])
    check("offset pages backwards through time",
          store.list_transactions(0, PAYER, limit=1, offset=1)[0]["d"]
          == f"sale {store.TX_MAX + 23}")
    check("a category filter selects",
          all(e["c"] == store.TX_MARKET_SALE
              for e in store.list_transactions(0, PAYER, limit=5,
                                               category=store.TX_MARKET_SALE)))
    check("an unknown category selects nothing",
          store.list_transactions(0, PAYER, limit=5, category="nope") == [])
    newest[0]["d"] = "tampered"
    check("the caller gets copies, not the live entries",
          store.list_transactions(0, PAYER, limit=1)[0]["d"] != "tampered")

    print("\ngarnishment writes both sides")
    reset()
    await store.add_debt(0, DEBTOR, str(CREDITOR), 500)
    await store.add_balance(0, DEBTOR, 400, garnishable=True,
                            category=store.TX_CONTRACT_PAYMENT, detail="Delivered")
    skim = [e for e in ledger(DEBTOR) if e["c"] == store.TX_DEBT_REPAYMENT]
    got = [e for e in ledger(CREDITOR) if e["c"] == store.TX_FINE_RECEIVED]
    check("the debtor's side is its own entry, not a smaller payout",
          len(skim) == 1 and skim[0]["a"] < 0, ledger(DEBTOR))
    check("the payout is still recorded in full",
          any(e["c"] == store.TX_CONTRACT_PAYMENT and e["a"] == 400
              for e in ledger(DEBTOR)), ledger(DEBTOR))
    check("the debtor's ledger reconciles after the skim",
          sum(e["a"] for e in ledger(DEBTOR)) == store.get_user(0, DEBTOR)["balance"],
          (ledger(DEBTOR), store.get_user(0, DEBTOR)["balance"]))
    check("the creditor gets an entry explaining the arrival",
          len(got) == 1 and got[0]["a"] == -skim[0]["a"], ledger(CREDITOR))
    check("naming the debtor", got[0]["p"] == str(DEBTOR), got[0])
    check("the creditor's ledger reconciles too",
          sum(e["a"] for e in ledger(CREDITOR)) == store.get_user(0, CREDITOR)["balance"])

    print("\nan untagged movement is recorded rather than dropped")
    reset(0)
    await store.add_balance(0, PAYER, 7)
    check("it lands under 'other'", ledger(PAYER)[-1]["c"] == store.TX_OTHER,
          ledger(PAYER)[-1])

    print("\nthe daily series covers every day, quiet ones included")
    reset()
    await store.add_balance(0, PAYER, 100, category=store.TX_REWARD)
    await store.try_debit(0, PAYER, 40, category=store.TX_MARKET_PURCHASE)
    series = store.transaction_series(0, PAYER, days=7)
    check("one bucket per day asked for", len(series) == 7, len(series))
    check("oldest first", [d["ts"] for d in series] == sorted(d["ts"] for d in series))
    check("today carries both directions",
          series[-1]["in"] == 100 and series[-1]["out"] == 40, series[-1])
    check("net is in minus out", all(d["net"] == d["in"] - d["out"] for d in series))
    check("quiet days are present rather than omitted",
          all(d["in"] == 0 and d["out"] == 0 for d in series[:-1]), series[:-1])
    check("the range is clamped to something sane",
          len(store.transaction_series(0, PAYER, days=0)) == 1
          and len(store.transaction_series(0, PAYER, days=9999)) == 365)

    print("\ntx_detail normalises a detail to one readable line")
    check("whitespace collapses", store_mod.tx_detail("  a\n  b  ") == "a b")
    check("empty falls back", store_mod.tx_detail("", "fallback") == "fallback")
    long = store_mod.tx_detail("word " * 40)
    check("a long one is cut at a word", len(long) <= 61 and long.endswith("…"), long)
    check("and cut anyway when there is no word break",
          store_mod.tx_detail("x" * 200).endswith("…"))

    print("\nthe vocabulary is reachable from the singleton call sites use")
    check("categories are on the instance",
          store.TX_CONTRACT_PAYMENT == store_mod.TX_CONTRACT_PAYMENT)
    check("so is the label table and the cap",
          store.TX_LABELS is store_mod.TX_LABELS and store.TX_MAX == store_mod.TX_MAX)
    check("every category has a label",
          all(c in store_mod.TX_LABELS
              for c in (v for k, v in vars(store_mod).items()
                        if k.startswith("TX_") and isinstance(v, str))))

    print("\nescrow is derived from the contracts, not from the ledger")
    import api_server
    from data import contracts as cdb

    ISSUER = str(PAYER)
    rows = [
        # Counted: every status in which the payment has not been released yet.
        {"issuer_id": ISSUER, "status": cdb.PENDING, "payment": 100},
        {"issuer_id": ISSUER, "status": cdb.ACTIVE, "payment": 50},
        {"issuer_id": ISSUER, "status": cdb.SUBMITTED, "payment": 25},
        {"issuer_id": ISSUER, "status": cdb.DISPUTED, "payment": 10},
        {"issuer_id": ISSUER, "status": cdb.MOD_REVIEW, "payment": 5},
        # Not counted: settled either way, so the money has already moved.
        {"issuer_id": ISSUER, "status": cdb.COMPLETED, "payment": 900},
        {"issuer_id": ISSUER, "status": cdb.CANCELLED, "payment": 900},
        # Not counted: they are the contractor here — nothing of theirs is held.
        {"issuer_id": str(PAYEE), "contractor_id": ISSUER,
         "status": cdb.ACTIVE, "payment": 700},
    ]
    auctions = [
        {"issuer_id": ISSUER, "start_value": 40},
        {"issuer_id": str(PAYEE), "start_value": 800},
    ]

    real_contracts, real_auctions = cdb.iter_user_contracts, api_server.aucdb.list_open
    # `**_kw` because the caller now narrows the query itself (statuses=/limit=,
    # added for UP21 so the escrow figure stops reading a whole contract history).
    # The stub deliberately ignores the filter and returns every row, which keeps
    # `_escrow_held`'s own status check under test rather than assuming the query
    # did it — the two must agree, and only one of them is in this process.
    cdb.iter_user_contracts = lambda gid, uid, **_kw: list(rows)
    api_server.aucdb.list_open = lambda gid: list(auctions)
    try:
        total, n_contracts, n_auctions = api_server._escrow_held(0, ISSUER)
        check("only unsettled contracts the player issued are counted",
              (total, n_contracts, n_auctions) == (230, 5, 1),
              f"{(total, n_contracts, n_auctions)}")

        # An issuer id stored as an int (older documents) must still match the
        # string the endpoint keys on, or the whole figure silently reads zero.
        cdb.iter_user_contracts = lambda gid, uid, **_kw: [
            {"issuer_id": PAYER, "status": cdb.ACTIVE, "payment": 60}]
        api_server.aucdb.list_open = lambda gid: []
        check("an int issuer id still matches",
              api_server._escrow_held(0, ISSUER) == (60, 1, 0))

        # One line on a summary card must never take the history tab down with it.
        def boom(*_a, **_k):
            raise RuntimeError("firestore is having a day")

        cdb.iter_user_contracts = boom
        api_server.aucdb.list_open = lambda gid: [{"issuer_id": ISSUER, "start_value": 7}]
        check("a failing read reports zero rather than raising",
              api_server._escrow_held(0, ISSUER) == (7, 0, 1))
    finally:
        cdb.iter_user_contracts = real_contracts
        api_server.aucdb.list_open = real_auctions

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
