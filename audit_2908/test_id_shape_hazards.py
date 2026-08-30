"""Account ids are not all snowflakes — where does a web-only (`a_…`) id still hit int()?

`data/accounts.py` gave website sign-ups an id of `a_<firebase uid>` and
`test_account_id_shapes.py` already proves the *caller's own* id survives every
endpoint. This script asks the other half of the question: what happens when the
web-only account is the OTHER party — the creditor of a fine, the issuer being
notified of a cancel, the winning bidder of an auction, the sender of a quicksent
vessel. Each of those is still an int() cast today.
"""
import asyncio

from _harness import check, section, finish, src, between, quiet
from data.store import store
import contract_actions as ca
import api_server

api_server._bot_user_id = 777
quiet(api_server)


async def garnish_case(creditor: str) -> tuple[Exception | None, int, int]:
    debtor = 9101
    u = store.get_user(0, debtor)
    u.update({"balance": 0, "debts": [], "tx": [], "tx_totals": {}})
    await store.add_debt(0, debtor, creditor, 100)
    raised = None
    try:
        await store.add_balance(0, debtor, 40, garnishable=True,
                                category=store.TX_CONTRACT_PAYMENT, detail="payout")
    except Exception as exc:          # noqa: BLE001 - the point is to see what escapes
        raised = exc
    return raised, store.debt_total(0, debtor), u["balance"]


async def main():
    section("garnishment when the creditor is a website account")
    raised, debt, bal = await garnish_case("9102")
    check("control: a snowflake creditor is paid (debt 80, wallet 20)",
          raised is None and debt == 80 and bal == 20, f"{raised!r} debt={debt} bal={bal}")

    raised, debt, bal = await garnish_case("a_webissuer")
    check("a fine owed to a web-only issuer is garnished without raising",
          raised is None, f"raised {raised!r}")
    check("the debt ledger and the wallet agree after the attempt "
          "(either untouched, or debt 80 with wallet 20)",
          (debt, bal) in {(100, 40), (80, 20)},
          f"debt={debt} wallet={bal}: {100 - debt} coins of debt were written off while "
          f"the full credit stayed in the wallet, and the exception escapes to the caller "
          f"(a contract review that has already flipped to COMPLETED)")

    section("cancel's counterparty lookup")
    ok = True
    try:
        ca._other_party({"issuer_id": "a_webissuer", "contractor_id": "123"}, "123")
    except ValueError:
        ok = False
    check("contract_actions._other_party survives a web-only issuer "
          "(cancel() calls it after the escrow refund has landed)", ok)

    section("auctions")
    auc = src("cogs/auctions.py")
    close = between(auc, "async def close_auction", "\n\n\n")
    check("close_auction does not int() the winning bidder id",
          "int(winner_id)" not in close,
          "a web-only account can bid via /api/v1/web/auctions/{id}/bid "
          "(try_place_bid stores str(bidder_id) with no shape check); close_auction then "
          "raises ValueError before the status write, so the auction stays OPEN forever "
          "and the issuer's escrow is never released")
    check("close_auction does not int() the issuer id", 'int(a["issuer_id"])' not in close)
    bid = src("data/auctions.py")
    tpb = between(bid, "def try_place_bid", "\n\n\n")
    check("try_place_bid refuses or tolerates a non-snowflake bidder",
          "isdigit" in tpb or "_discord_id" in tpb)

    section("friend quicksend")
    api = src("api_server.py")
    reject = between(api, "async def craft_gift_reject", '@app.post("/api/v1/craft/send")')
    accept = between(api, "async def craft_gift_accept", "async def craft_gift_reject")
    check("declining a gift does not int() the sender id", "int(sender_id)" not in reject,
          "a live vessel quicksent by a web-origin account and declined: the offer is "
          "deleted, then int('a_…') raises before the return enqueue — the ship is gone "
          "from the sender's save and nothing gives it back")
    check("accepting a gift does not int() the sender id", "int(sender_id)" not in accept)
    check("the decline path re-queues the returned vessel BEFORE deleting the offer",
          reject.index("imp.enqueue") < reject.index("imp.delete("),
          "delete runs first, so any failure between the two loses the vessel")

    section("moderation surfaces that still key on a snowflake")
    adj = between(api, "async def admin_user_adjust", "\n\n\n")
    check("admin console adjust accepts a web-only account id",
          "isdigit" not in adj, "documented gap in CLAUDE.md; listed for completeness")

    finish()


asyncio.run(main())
