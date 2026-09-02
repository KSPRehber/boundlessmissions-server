"""Offline exercise of contract_actions: authorization, state gates, money flow.

Everything below the service is stubbed — no Firestore, no Discord — so this checks
the decisions the module makes, not the plumbing under it.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contract_actions as ca
import rewards
import settings
from data import contracts as cdb
import api_server

BOT = 999
GID = 1
# Account ids are strings — the real store has always keyed on `str(user_id)`, and
# since accounts a website sign-up's id is not numeric at all. The ints these used
# to be were only ever cosmetic.
ISSUER = "100"
CONTRACTOR = "200"
STRANGER = "300"

DB = {}
BAL = {}
EVENTS = []
# user id → {creditor id → amount owed}; "" is a bot-issued fine with no creditor.
DEBT = {}
# (user id, amount) for every credit flagged as earnings, so a test can assert that
# a refund was not one.
GARNISHABLE = []


def _mk(status=cdb.PENDING, **over):
    c = {
        "contract_id": "c1", "guild_id": str(GID),
        "issuer_id": str(ISSUER), "issuer_name": "Issuer",
        "contractor_id": str(CONTRACTOR), "contractor_name": "Contractor",
        "mission": "Land on the Mun", "payment": 100, "fine": 40,
        "due_date": "2099-01-01", "status": status,
    }
    c.update(over)
    DB["c1"] = c
    return c


# ── stubs ────────────────────────────────────────────────────────────────────
cdb.get_contract = lambda gid, cid: dict(DB[cid]) if cid in DB else None


def _update(gid, cid, **fields):
    DB[cid].update(fields)


cdb.update_contract = _update


from data import store as real_store


class _Store:
    """Keys on str(uid), exactly as data/store.py does.

    The ledger kwargs (`category` / `detail` / `counterparty`) are accepted and
    ignored: what they record is tested in test_debt.py against the real store,
    while what matters here is which credits are flagged garnishable. Accepting
    them is not optional though — every money call in contract_actions passes
    them now, so a stub without them fails with a TypeError rather than a
    meaningful assertion.
    """

    # Real module constants, not copies: a category renamed in the store must not
    # keep passing here against a stale duplicate.
    TX_OTHER = real_store.TX_OTHER
    TX_CONTRACT_PAYMENT = real_store.TX_CONTRACT_PAYMENT
    TX_CONTRACT_REFUND = real_store.TX_CONTRACT_REFUND
    TX_CONTRACT_FINE = real_store.TX_CONTRACT_FINE
    TX_FINE_RECEIVED = real_store.TX_FINE_RECEIVED
    tx_detail = staticmethod(real_store.tx_detail)

    async def add_balance(self, gid, uid, amt, *, garnishable=False,
                          category="", detail="", counterparty=""):
        uid = str(uid)
        BAL[uid] = BAL.get(uid, 0) + amt
        # Mirror the real store closely enough to catch a caller that garnishes a
        # refund: the tests assert on which credits were flagged, not on the split.
        if garnishable and amt > 0:
            GARNISHABLE.append((uid, amt))
        return BAL[uid]

    def debt_total(self, gid, uid):
        return sum(DEBT.get(str(uid), {}).values())

    def has_user(self, uid):
        # `_pay_issuer` skips an issuer whose record was deleted. Nothing in this
        # suite deletes an account, so every id here is live — and keying on BAL
        # would silently drop refunds to issuers a test never seeded.
        return True

    async def add_debt(self, gid, uid, creditor_id, amount):
        d = DEBT.setdefault(str(uid), {})
        d[str(creditor_id or "")] = d.get(str(creditor_id or ""), 0) + amount
        return sum(d.values())

    async def try_debit(self, gid, uid, amt, *, category="", detail="",
                        counterparty=""):
        uid = str(uid)
        if BAL.get(uid, 0) < amt:
            return False
        BAL[uid] -= amt
        return True

    async def debit_up_to(self, gid, uid, amt, *, category="", detail="",
                          counterparty=""):
        uid = str(uid)
        take = min(BAL.get(uid, 0), amt)
        BAL[uid] = BAL.get(uid, 0) - take
        return take

    async def add_rescue(self, gid, uid, amount=1):
        return 1


ca.store = _Store()
api_server._bot_user_id = BOT
api_server._bot_instance = None
api_server._create_notification = lambda *a, **k: EVENTS.append(("notify", a[1], a[2]))


async def _deliver(gid, cid, c):
    EVENTS.append(("deliver_rescue", cid))


async def _restore(gid, cid, c):
    EVENTS.append(("restore_vessel", cid))


api_server._deliver_rescue_craft = _deliver
api_server._restore_issuer_vessel = _restore


# XP is a cross-module effect like the rescue hand-offs above: this file checks
# that approving awards it, not what `rewards` does with it (see test_rewards.py).
async def _grant_xp(gid, uid, amount, *, reason=""):
    if amount > 0:
        EVENTS.append(("xp", uid, amount))
    return amount, False


ca.rewards.grant_xp = _grant_xp


# ── assertions ───────────────────────────────────────────────────────────────
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


async def main():
    global BAL, EVENTS, DEBT, GARNISHABLE

    print("\naccept")
    _mk()
    r = await ca.accept(GID, "c1", actor_id=STRANGER, actor_name="Nosy")
    check("a stranger cannot accept", not r.ok and r.code == ca.FORBIDDEN, r.code)
    check("status untouched after refusal", DB["c1"]["status"] == cdb.PENDING)
    r = await ca.accept(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("the issuer cannot accept their own offer", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.accept(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("the contractor can accept", r.ok and DB["c1"]["status"] == cdb.ACTIVE)
    r = await ca.accept(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("accepting twice is refused", not r.ok and r.code == ca.BAD_STATE)
    r = await ca.accept(GID, "nope", actor_id=CONTRACTOR, actor_name="Contractor")
    check("missing contract is not_found", not r.ok and r.code == ca.NOT_FOUND)

    print("\ncancel")
    _mk(status=cdb.ACTIVE)
    BAL = {}
    r = await ca.cancel(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor cannot cancel an active contract for free",
          not r.ok and r.code == ca.USE_GIVE_UP, r.code)
    check("no refund was paid on that refusal", BAL.get(ISSUER, 0) == 0)
    r = await ca.cancel(GID, "c1", actor_id=STRANGER, actor_name="Nosy")
    check("a stranger cannot cancel", not r.ok and r.code == ca.FORBIDDEN)
    DEBT.clear()
    r = await ca.cancel(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("issuer can withdraw an active contract", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    # Withdrawing after the contractor accepted is not free: the agreed fine goes
    # to the contractor. The issuer had nothing on hand, but the escrow comes back
    # BEFORE the fine is collected, so it is paid out of the refund rather than
    # becoming a debt the issuer holds the money for.
    check("escrow refunded once, fine paid out of it",
          BAL.get(ISSUER) == 60 and BAL.get(CONTRACTOR) == 40, BAL)
    check("the withdrawal fine was collected, not owed",
          not DEBT.get(str(ISSUER)) and r.data.get("fine_collected") == 40
          and r.data.get("fine_owed") == 0, (DEBT, r.data))
    check("the contractor's fine receipt is earnings", (str(CONTRACTOR), 40) in GARNISHABLE,
          GARNISHABLE)
    r = await ca.cancel(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("cancelling twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second refund", BAL.get(ISSUER) == 60, BAL)

    _mk(status=cdb.PENDING)
    BAL = {}
    r = await ca.cancel(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor may decline a pending offer", r.ok and DB["c1"]["status"] == cdb.CANCELLED)

    print("\ncancel: bot issuer")
    _mk(status=cdb.ACTIVE, issuer_id=str(BOT))
    BAL = {}
    await ca.cancel(GID, "c1", actor_id=BOT, actor_name="Bot")
    check("a bot issuer is never credited", BAL.get(BOT, 0) == 0, BAL)

    print("\ngive_up")
    # Backing out used to be refused outright when the contractor could not cover the
    # fine, which left the contract ACTIVE with no exit — while submitting junk and
    # waiting out the dispute clock took the same partial amount three days later.
    _mk(status=cdb.ACTIVE)
    BAL, DEBT, GARNISHABLE = {CONTRACTOR: 10}, {}, []
    r = await ca.give_up(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("give up succeeds without the full fine", r.ok, r.code)
    check("contract closed anyway", DB["c1"]["status"] == cdb.CANCELLED)
    check("what they had was taken", BAL[CONTRACTOR] == 0, BAL)
    check("the shortfall is owed to the issuer",
          DEBT[str(CONTRACTOR)][str(ISSUER)] == 30, DEBT)
    check("the issuer got escrow + what was collected", BAL[ISSUER] == 110, BAL)
    check("only the fine half was garnishable",
          GARNISHABLE == [(str(ISSUER), 10)], GARNISHABLE)

    print("\ngive_up: a bot fine is owed to nobody")
    _mk(status=cdb.ACTIVE, issuer_id=str(BOT))
    BAL, DEBT, GARNISHABLE = {CONTRACTOR: 0}, {}, []
    await ca.give_up(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("debt is filed under an empty creditor",
          DEBT[str(CONTRACTOR)] == {"": 40}, DEBT)
    check("the bot is never credited", BAL.get(str(BOT), 0) == 0, BAL)

    _mk(status=cdb.ACTIVE)
    BAL, DEBT, GARNISHABLE = {CONTRACTOR: 50}, {}, []
    r = await ca.give_up(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("the issuer cannot give up", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.give_up(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor gives up", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    check("fine debited", BAL[CONTRACTOR] == 10, BAL)
    check("issuer paid fine + escrow", BAL[ISSUER] == 140, BAL)
    check("no debt when the fine was covered", not DEBT.get(str(CONTRACTOR)), DEBT)

    print("\nreview")
    _mk(status=cdb.SUBMITTED)
    BAL, EVENTS = {}, []
    r = await ca.review(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot approve their own submission",
          not r.ok and r.code == ca.FORBIDDEN)
    check("nothing was paid", BAL == {}, BAL)
    check("no XP for a refused approval",
          not [e for e in EVENTS if e[0] == "xp"], EVENTS)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("issuer approves", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("contractor paid", BAL[CONTRACTOR] == 100, BAL)
    # A player-issued contract earns no XP unless settings.CONTRACT_XP_HUMAN_ISSUED
    # is on: the issuer is the only judge of the work, and two cooperating accounts
    # could cycle one contract for unbounded XP and level-up coins (2908 audit, F3).
    # grant_xp is a no-op for 0, so no "xp" event is the expected shape.
    xp_expected = rewards.contract_xp(100)
    check("contractor earned exactly the configured XP (none by default)",
          (("xp", CONTRACTOR, xp_expected) in EVENTS) == (xp_expected > 0), EVENTS)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("approving twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no double payment", BAL[CONTRACTOR] == 100, BAL)
    check("no double XP",
          len([e for e in EVENTS if e[0] == "xp"]) == (1 if xp_expected > 0 else 0), EVENTS)

    print("\nreview: rescue delivery")
    _mk(status=cdb.SUBMITTED, mission_type=cdb.RESCUE, issuer_vessel_removed=True)
    BAL, EVENTS = {}, []
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("approving a rescue delivers the craft",
          ("deliver_rescue", "c1") in EVENTS, EVENTS)

    print("\ndispute")
    _mk(status=cdb.DISPUTED)
    BAL = {CONTRACTOR: 500}
    r = await ca.dispute(GID, "c1", actor_id=ISSUER, actor_name="I", action="pay_fine")
    check("the issuer cannot drive the dispute", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="nonsense")
    check("an unknown action is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("pay_fine closes the contract", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("fine + escrow to issuer", BAL[ISSUER] == 140 and BAL[CONTRACTOR] == 460, BAL)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("paying the fine twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("balance unchanged", BAL[CONTRACTOR] == 460, BAL)

    print("\ndispute: conceding is possible while broke")
    _mk(status=cdb.DISPUTED)
    BAL, DEBT, GARNISHABLE = {CONTRACTOR: 5}, {}, []
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("pay_fine still closes the contract", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("the rest is billed", DEBT[str(CONTRACTOR)][str(ISSUER)] == 35, DEBT)

    print("\ndispute: rescue is handed back on pay_fine")
    _mk(status=cdb.DISPUTED, mission_type=cdb.RESCUE, issuer_vessel_removed=True)
    BAL, EVENTS = {CONTRACTOR: 100}, []
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("issuer's vessel restored", ("restore_vessel", "c1") in EVENTS, EVENTS)

    print("\ndispute: more_time")
    _mk(status=cdb.DISPUTED)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="not-a-date")
    check("a malformed date is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2020-01-01")
    check("a date in the past is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="**2099-01-02**")
    check("a markdown-wrapped date is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-01-02")
    check("a valid future date opens a request", r.ok, f"{r.code}: {r.message}")
    check("status still disputed until the issuer answers",
          DB["c1"]["status"] == cdb.DISPUTED)
    check("the request is recorded on the contract",
          (DB["c1"].get("pending_request") or {}).get("new_date") == "2099-01-02",
          DB["c1"].get("pending_request"))

    print("\ndispute: more_time on a bot contract self-approves")
    _mk(status=cdb.DISPUTED, issuer_id=str(BOT))
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="more_time")
    check("bot contract extends itself", r.ok and DB["c1"]["status"] == cdb.ACTIVE, r.message)
    check("a new due date was set", DB["c1"]["due_date"] == r.data["new_date"])

    print("\ndispute: settle on a bot contract")
    _mk(status=cdb.DISPUTED, issuer_id=str(BOT))
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("AI contracts cannot be settled", not r.ok and r.code == ca.BAD_REQUEST)

    print("\nsettle_response")
    _mk(status=cdb.DISPUTED)
    BAL = {}
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("the issuer cannot settle unilaterally", not r.ok and r.code == ca.BAD_STATE, r.code)
    check("nothing was refunded", BAL == {}, BAL)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("a settle request is recorded",
          (DB["c1"].get("pending_request") or {}).get("kind") == "settle")
    r = await ca.settle_response(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot approve their own settlement",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("a settle request cannot be answered as an extension",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("issuer settles", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    check("escrow returned", BAL[ISSUER] == 100, BAL)
    check("the request is cleared", DB["c1"].get("pending_request") is None)
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("settling twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second refund", BAL[ISSUER] == 100, BAL)

    print("\nsettle_response: refusing leaves it disputed")
    _mk(status=cdb.DISPUTED)
    BAL = {}
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refused", r.ok and DB["c1"]["status"] == cdb.DISPUTED)
    check("request cleared so the buttons come back",
          DB["c1"].get("pending_request") is None)
    check("no money moved", BAL == {}, BAL)

    print("\nmore_time_response")
    _mk(status=cdb.DISPUTED)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                     action="more_time", new_date="2099-05-05")
    r = await ca.more_time_response(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                                    approve=True)
    check("the contractor cannot approve their own extension",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I",
                                    approve=True, new_date="2099-12-31")
    check("issuer approves the extension",
          r.ok and DB["c1"]["status"] == cdb.ACTIVE)
    check("the date granted is the one requested, not the one passed in",
          DB["c1"]["due_date"] == "2099-05-05", DB["c1"]["due_date"])
    check("the request is cleared", DB["c1"].get("pending_request") is None)

    print("\nreview: issuer drops the dispute and accepts after all")
    _mk(status=cdb.SUBMITTED)
    BAL = {}
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("disputed", DB["c1"]["status"] == cdb.DISPUTED)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    r = await ca.review(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot accept their own work",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refusing an already-disputed contract is refused",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("issuer accepts after all", r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("contractor paid in full", BAL[CONTRACTOR] == 100, BAL)
    check("no fine was taken", CONTRACTOR not in BAL or BAL[CONTRACTOR] == 100)
    check("the open settle request is cleared",
          DB["c1"].get("pending_request") is None)
    check("and it is off the auto-fine clock", ca.auto_fine_at(DB["c1"]) is None)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("accepting twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no double payment", BAL[CONTRACTOR] == 100, BAL)

    print("\nreview: an escalated case stays with the moderators")
    _mk(status=cdb.MOD_REVIEW)
    BAL = {}
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("the issuer cannot accept out from under a mod review",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    check("nothing was paid", BAL == {}, BAL)

    print("\ndispute clock: one extension request per dispute")
    _mk(status=cdb.SUBMITTED)
    BAL = {}
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refusing stamps the dispute clock", bool(DB["c1"].get("disputed_at")))
    check("the extension allowance is reset", DB["c1"].get("more_time_requests") == 0)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-03-03")
    check("first extension request is allowed", r.ok, r.message)
    await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("still disputed after a refusal", DB["c1"]["status"] == cdb.DISPUTED)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-04-04")
    check("a second request is refused even after the first was denied",
          not r.ok and r.code == ca.BAD_STATE, r.message)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("settle is still available", r.ok, r.message)

    print("\ndispute clock: a granted extension restores the allowance")
    _mk(status=cdb.SUBMITTED)
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                     action="more_time", new_date="2099-03-03")
    await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("granted, back to active", DB["c1"]["status"] == cdb.ACTIVE)
    DB["c1"]["status"] = cdb.SUBMITTED           # contractor submits again
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("a fresh dispute resets the allowance",
          DB["c1"].get("more_time_requests") == 0)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-06-06")
    check("so another request is allowed", r.ok, r.message)

    print("\ndispute timeout")
    _mk(status=cdb.DISPUTED)
    BAL = {CONTRACTOR: 500}
    r = await ca.expire_dispute(GID, "c1")
    check("a dispute with no clock is stamped, not fined",
          not r.ok and DB["c1"].get("disputed_at"), r.message)
    check("no money moved", BAL[CONTRACTOR] == 500, BAL)
    r = await ca.expire_dispute(GID, "c1")
    check("and it is not yet due", not r.ok and r.code == ca.BAD_STATE, r.message)

    fresh = datetime.utcnow() - timedelta(days=settings.DISPUTE_AUTO_FINE_DAYS - 1)
    DB["c1"]["disputed_at"] = fresh.isoformat()
    r = await ca.expire_dispute(GID, "c1")
    check("inside the window it is left alone", not r.ok and r.code == ca.BAD_STATE)
    check("still disputed", DB["c1"]["status"] == cdb.DISPUTED)

    stale = datetime.utcnow() - timedelta(days=settings.DISPUTE_AUTO_FINE_DAYS, hours=1)
    DB["c1"]["disputed_at"] = stale.isoformat()
    r = await ca.expire_dispute(GID, "c1")
    check("past the window the fine is collected", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("contractor charged the fine", BAL[CONTRACTOR] == 460, BAL)
    check("issuer paid fine + escrow", BAL[ISSUER] == 140, BAL)
    r = await ca.expire_dispute(GID, "c1")
    check("expiring twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second collection", BAL[CONTRACTOR] == 460, BAL)

    print("\ndispute timeout: a broke contractor cannot stall forever")
    _mk(status=cdb.DISPUTED,
        disputed_at=(datetime.utcnow() - timedelta(days=30)).isoformat())
    BAL = {CONTRACTOR: 5}
    r = await ca.expire_dispute(GID, "c1")
    check("closed anyway", r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("took what they had", BAL[CONTRACTOR] == 0, BAL)
    check("issuer credited only what was actually collected",
          BAL[ISSUER] == 105, BAL)

    print("\ndispute timeout: an unanswered request is not the contractor's fault")
    # The clock does not pause — but a request the issuer never answered goes to
    # the moderators rather than to the fine (2908 audit, F8). With no moderator
    # channel configured (as here), the request is cleared and the clock restarts
    # exactly once; the second time it runs out, the agreed fine lands.
    _mk(status=cdb.DISPUTED,
        disputed_at=(datetime.utcnow() - timedelta(days=30)).isoformat())
    BAL = {CONTRACTOR: 500}
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("a settle request is open", DB["c1"].get("pending_request") is not None)
    r = await ca.expire_dispute(GID, "c1")
    check("an unanswered request is not fined", not r.ok and BAL[CONTRACTOR] == 500
          and DB["c1"]["status"] == cdb.DISPUTED, r.message)
    check("the request is cleared", DB["c1"].get("pending_request") is None)
    check("the clock restarted once", DB["c1"].get("request_grace_used") is True)
    DB["c1"]["disputed_at"] = (datetime.utcnow() - timedelta(days=30)).isoformat()
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    r = await ca.expire_dispute(GID, "c1")
    check("a second unanswered request does not restart it again: the fine lands",
          r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("the request is cleared", DB["c1"].get("pending_request") is None)

    print("\noverdue: an unsubmitted contract cannot sit ACTIVE forever")
    from datetime import timedelta as _td
    _tz = timezone(timedelta(hours=3))
    _today = datetime.now(_tz).date()
    _mk(status=cdb.ACTIVE, due_date="2099-01-01")
    BAL, DEBT, EVENTS = {}, {}, []
    r = await ca.expire_overdue(GID, "c1")
    check("a contract in date is left alone", not r.ok and r.code == ca.BAD_STATE)

    _mk(status=cdb.ACTIVE,
        due_date=str(_today - _td(days=settings.CONTRACT_OVERDUE_GRACE_DAYS)))
    r = await ca.expire_overdue(GID, "c1")
    check("the grace period is honoured", not r.ok and r.code == ca.BAD_STATE, r.message)
    check("still active", DB["c1"]["status"] == cdb.ACTIVE)

    _mk(status=cdb.ACTIVE,
        due_date=str(_today - _td(days=settings.CONTRACT_OVERDUE_GRACE_DAYS + 1)))
    BAL, DEBT, EVENTS = {}, {}, []
    r = await ca.expire_overdue(GID, "c1")
    check("past it the contract goes to dispute",
          r.ok and DB["c1"]["status"] == cdb.DISPUTED, r.message)
    check("nothing was charged", BAL == {} and DEBT == {}, (BAL, DEBT))
    check("the auto-fine clock is now running", ca.auto_fine_at(DB["c1"]) is not None)
    check("both parties were told", len([e for e in EVENTS if e[0] == "notify"]) == 2, EVENTS)
    r = await ca.expire_overdue(GID, "c1")
    check("sweeping twice is refused", not r.ok and r.code == ca.BAD_STATE)

    _mk(status=cdb.SUBMITTED, due_date="2000-01-01")
    r = await ca.expire_overdue(GID, "c1")
    check("a submitted contract is never swept — that wait is the issuer's",
          not r.ok and r.code == ca.BAD_STATE)

    _mk(status=cdb.ACTIVE, due_date="")
    r = await ca.expire_overdue(GID, "c1")
    check("a contract with no due date is left alone",
          not r.ok and r.code == ca.BAD_REQUEST)

    print("\nmod_resolve")
    _mk(status=cdb.MOD_REVIEW)
    BAL = {CONTRACTOR: 25}
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=True)
    check("enforced", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("only what the contractor had was taken", BAL[CONTRACTOR] == 0, BAL)
    check("issuer credited exactly that plus escrow", BAL[ISSUER] == 125, BAL)
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=True)
    check("resolving twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second payout", BAL[ISSUER] == 125, BAL)

    _mk(status=cdb.MOD_REVIEW)
    BAL = {CONTRACTOR: 500}
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=False)
    check("cancelling the fine takes nothing", BAL[CONTRACTOR] == 500, BAL)
    check("escrow refunded", BAL[ISSUER] == 100, BAL)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(asyncio.run(main()))
