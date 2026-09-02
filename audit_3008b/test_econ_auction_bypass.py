"""Auction close binds the winner to an ACTIVE contract without the gates
`contract_actions.accept` applies.

  A. DEBT_MAX_OUTSTANDING is checked only in `accept`; an auction winner never
     accepts — `_close_auction_locked` writes ACTIVE directly. Same for the
     weekly-mission select path (source check).
  B. MAX_FINE_MULTIPLE_OF_PAYMENT is checked against `start_value` at open time,
     but the contract's payment is the *winning bid*. Start 10_000 / fine 50_000
     / winning bid 1 → a contract paying 1 with a 50_000 fine (50_000× the
     payment, cap is 5×).
  C. MAX_ACTIVE_CONTRACTS_PER_USER is not checked for the winner either.
"""
import types
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, mk, wallet, bal, run, store, ca, cdb, GID, DB, settings, src, between
import cogs.auctions as auc
from data import auctions as adb

AUC = {}
adb.get_auction = lambda gid, aid: dict(AUC[aid]) if aid in AUC else None
adb.update_auction = lambda gid, aid, **f: AUC[aid].update(f)


async def _noop(*a, **k):
    return None
auc._edit_auction_message = _noop
auc._embed = lambda c, gid: types.SimpleNamespace(description="")


def auction(aid, issuer, bidder, start, bid, fine):
    AUC[aid] = {"auction_id": aid, "guild_id": str(GID), "issuer_id": issuer, "issuer_name": "I",
                "mission": "m", "start_value": start, "current_bid": bid,
                "current_bidder_id": bidder, "current_bidder_name": "B", "bid_count": 1,
                "fine": fine, "due_date": "2099-01-01", "status": adb.OPEN, "mirrors": []}


async def main():
    section("A. auction winner over DEBT_MAX_OUTSTANDING is still bound to an ACTIVE contract")
    cap = settings.DEBT_MAX_OUTSTANDING
    wallet("100", balance=0); wallet("200", balance=0, debts=[{"creditor_id": "300", "amount": cap + 1}])
    # The same debtor cannot accept a plain offer:
    mk("offer", status=cdb.PENDING, issuer="100", contractor="200")
    r = await ca.accept(GID, "offer", actor_id="200", actor_name="B")
    check("control: accept() refuses a debtor over the cap", not r.ok and r.code == ca.DEBT_LIMIT)
    auction("a1", "100", "200", start=500, bid=400, fine=100)
    await auc.close_auction(None, GID, "a1")
    bound = [c for c in DB.values() if c["contractor_id"] == "200" and c["status"] == cdb.ACTIVE]
    check("auction close does not bind a debtor over DEBT_MAX_OUTSTANDING", not bound,
          f"contract {bound[0]['contract_id'] if bound else ''} ACTIVE for a debtor owing {cap+1} "
          f"(cap {cap}); no debt_total check in cogs/auctions._close_auction_locked")
    whole = src("api_server.py")
    i = whole.index("_save_selection(gid, wk, uid, req.mission_id) is False")
    sel = whole[whole.rfind("\n@app.", 0, i):whole.index("\n@app.", i)]
    check("weekly mission select applies DEBT_MAX_OUTSTANDING (source)",
          "DEBT_MAX_OUTSTANDING" in sel or "debt_total" in sel,
          "mission select creates the bot contract and writes status=ACTIVE with no debt check")

    section("B. fine multiple is judged against start_value, not the bid that becomes the payment")
    wallet("100", balance=0); wallet("200", balance=0)
    auction("a2", "100", "200", start=10_000, bid=1, fine=50_000)   # 50_000 <= 5 x 10_000 at open time
    await auc.close_auction(None, GID, "a2")
    c = next(c for c in DB.values() if c.get("contract_id") == AUC["a2"]["result_contract_id"])
    mult = settings.MAX_FINE_MULTIPLE_OF_PAYMENT
    check("winner's contract respects MAX_FINE_MULTIPLE_OF_PAYMENT",
          c["fine"] <= c["payment"] * mult,
          f"payment {c['payment']}, fine {c['fine']} = {c['fine']//max(1,c['payment'])}x (cap {mult}x); "
          f"a give_up/dispute timeout now leaves a {c['fine']} debt on a {c['payment']}-coin job")

    section("C. winner's active-contract cap")
    wallet("100", balance=0); wallet("200", balance=0)
    # Sections A and B leave two of the winner's contracts behind (a PENDING offer
    # and B's contract); the premise here is "exactly at the cap", so start clean.
    for k in [k for k, c in DB.items() if c["contractor_id"] == "200"]:
        DB.pop(k)
    for i in range(settings.MAX_ACTIVE_CONTRACTS_PER_USER):
        mk(f"full{i}", status=cdb.ACTIVE, issuer="100", contractor="200")
    auction("a3", "100", "200", start=50, bid=10, fine=0)
    await auc.close_auction(None, GID, "a3")
    n = cdb.count_active(GID, "200")
    check("auction close honours MAX_ACTIVE_CONTRACTS_PER_USER for the winner",
          n <= settings.MAX_ACTIVE_CONTRACTS_PER_USER, f"{n} active for the winner (cap {settings.MAX_ACTIVE_CONTRACTS_PER_USER})")
    finish()

run(main())
