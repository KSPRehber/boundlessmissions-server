"""Check-then-act across an await: the two endpoints that mint coins.

`contract_actions` transitions read, check and write Firestore synchronously, so
inside one process they cannot interleave (a control below proves it). Two
handlers in api_server are different — they await real I/O between the status
check and the write that would stop a second copy:

  * POST /api/v1/missions/select  — `_has_selected` … await _classify_missions …
                                    create_contract … `_save_selection`
  * POST /api/v1/contracts/{id}/submit — `status == ACTIVE` … await uploads,
                                    ban check, AI review … COMPLETED + payout

Weekly-mission coins are minted from nothing, so each extra copy is new money.
The handlers are called directly (no HTTP) with the storage layer faked, so the
awaits that matter — uploads, classification — are simulated with a short sleep.
"""
import asyncio
import copy
import io
import uuid

from starlette.datastructures import UploadFile, Headers

from _harness import check, section, finish, quiet
import settings
import api_server
import contract_actions as ca
import rewards
import cogs.weeklymissions as wm
import cogs.corps as corps
import cogs.screenshots as shots
from data import contracts as cdb
from data.store import store

api_server._bot_user_id = 777
quiet(api_server)

# ── fake contract storage (shared by api_server.cdb and contract_actions.cdb) ──
CONTRACTS: dict[str, dict] = {}


def fake_get(gid, cid):
    c = CONTRACTS.get(cid)
    return copy.deepcopy(c) if c else None


def fake_update(gid, cid, **fields):
    CONTRACTS[cid].update(fields)


def fake_create(guild_id, issuer_id, issuer_name, contractor_id, contractor_name,
                mission, payment, fine, due_date, modlist=None, **kw):
    cid = uuid.uuid4().hex[:12]
    doc = {"contract_id": cid, "guild_id": str(guild_id), "issuer_id": str(issuer_id),
           "issuer_name": issuer_name, "contractor_id": str(contractor_id),
           "contractor_name": contractor_name, "mission": mission, "payment": payment,
           "fine": fine, "due_date": due_date, "status": cdb.PENDING, "modlist": modlist}
    doc.update(kw)
    CONTRACTS[cid] = doc
    return copy.deepcopy(doc)


cdb.get_contract = fake_get
cdb.update_contract = fake_update
cdb.create_contract = fake_create


async def slow_upload(*a, **k):
    await asyncio.sleep(0.05)          # one Storage round trip
    return "https://storage.invalid/obj"


cdb.upload_to_storage = slow_upload
cdb.upload_private_to_storage = slow_upload
# The reviewer approves. (An *unavailable* reviewer no longer auto-accepts — the
# submission is held for a moderator — so the race is only meaningful with one.)
import json, types


class _Models:
    def generate_content(self, model, contents, config):
        return types.SimpleNamespace(text=json.dumps({"approved": True, "reason": "ok"}),
                                     usage_metadata=None)


shots.active_client = lambda: types.SimpleNamespace(models=_Models())


async def _dl(url):
    return b"\x89PNG"


cdb.download_url = _dl
api_server._rate_limit = lambda *a, **k: None


def wallet(uid):
    u = store.get_user(0, uid)
    return u["balance"], u["xp"]


def reset_user(uid):
    u = store.get_user(0, uid)
    u.update({"balance": 0, "xp": 0, "level": 0, "debts": [], "tx": [], "tx_totals": {}})


def png_upload():
    return UploadFile(io.BytesIO(b"\x89PNG fake"), filename="shot.png",
                      headers=Headers({"content-type": "image/png"}))


async def submit(cid, user):
    return await api_server.submit_contract(
        cid, craft_file=None, vessel_node=None, loadmeta=None,
        vessel_data='{"body": "Kerbin", "situation": "ORBITING"}',
        screenshot1=png_upload(), screenshot2=None, screenshot3=None, screenshots=[],
        modlist=None, used_modlist=None, used_parts=None, delta_v_vac=None,
        life_support=None, ls_endurance_days=0.0, ls_crew_capacity=0,
        cheat_report=None, user=user)


async def main():
    # ── weekly mission selection ────────────────────────────────────────────
    section("POST /missions/select fired 5× in parallel for the same mission")
    MISSION = {"id": 3, "desc_en": "Orbit Kerbin", "coins": 600, "fine": 100, "xp": 1000,
               "difficulty": 10, "mission_type": "active_vessel"}
    selected: set = set()
    wm._is_locked = lambda now=None: False
    wm._load_missions = lambda gid, wk: ([MISSION], 1)
    wm._has_selected = lambda gid, wk, uid, mid: (gid, wk, str(uid), mid) in selected
    wm._save_selection = lambda gid, wk, uid, mid: selected.add((gid, wk, str(uid), mid))
    corps._get_corp = lambda gid, uid: {"channel_id": "1", "owner_name": "Racer"}

    async def slow_classify(missions, wk):
        await asyncio.sleep(0.05)      # the Firestore/Gemini hop
        return missions

    api_server._classify_missions = slow_classify
    user = {"guild_id": "0", "user_id": "9201", "username": "Racer"}
    req = api_server.MissionSelectRequest(mission_id=3)
    results = await asyncio.gather(*[api_server.select_mission(req, user) for _ in range(5)])
    accepted = sum(1 for r in results if r.success)
    mine = [c for c in CONTRACTS.values() if c["contractor_id"] == "9201"]
    check("one weekly mission yields one contract per player",
          accepted == 1 and len(mine) == 1,
          f"{accepted} accepted, {len(mine)} contracts created — each pays "
          f"{MISSION['coins']} minted coins on approval")

    # ── submission double-pay ───────────────────────────────────────────────
    section("POST /contracts/{id}/submit fired 3× in parallel on a bot-issued contract")
    reset_user("9202")
    CONTRACTS["c1"] = {"contract_id": "c1", "guild_id": "0", "issuer_id": "777",
                       "issuer_name": "Boundless Missions", "contractor_id": "9202",
                       "contractor_name": "Pilot", "mission": "Orbit Kerbin", "payment": 600,
                       "fine": 0, "due_date": "2099-01-01", "status": cdb.ACTIVE}
    user2 = {"guild_id": "0", "user_id": "9202", "username": "Pilot"}
    results = await asyncio.gather(*[submit("c1", user2) for _ in range(3)])
    approved = sum(1 for r in results if getattr(r, "success", False)
                   and getattr(r, "review_status", "") == "approved")
    bal, xp = wallet("9202")
    payouts = store.list_transactions(0, "9202", category=store.TX_CONTRACT_PAYMENT)
    check("a contract pays its reward once however many submits race",
          len(payouts) == 1, f"{len(payouts)} payouts of 600 landed (balance={bal}, "
          f"{approved} approvals returned)")
    expected_xp = rewards.contract_xp(600, bot_issued=True)   # c1 is issued by the bot
    check("XP is granted once", xp == expected_xp, f"xp={xp}, expected {expected_xp}")

    # ── control: the transition module really is atomic in-process ─────────
    section("control: contract_actions.review(approve) ×2 in parallel")
    reset_user("9203")
    reset_user("9204")
    CONTRACTS["c2"] = {"contract_id": "c2", "guild_id": "0", "issuer_id": "9203",
                       "issuer_name": "Boss", "contractor_id": "9204",
                       "contractor_name": "Worker", "mission": "Build a rover",
                       "payment": 300, "fine": 0, "due_date": "2099-01-01",
                       "status": cdb.SUBMITTED}
    await asyncio.gather(*[ca.review(0, "c2", actor_id="9203", actor_name="Boss", approve=True)
                           for _ in range(2)])
    payouts = store.list_transactions(0, "9204", category=store.TX_CONTRACT_PAYMENT)
    check("issuer approving twice at once pays the contractor once",
          len(payouts) == 1, f"{len(payouts)} payouts (balance={wallet('9204')[0]})")

    finish()


asyncio.run(main())
