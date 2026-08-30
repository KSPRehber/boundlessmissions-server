"""Shared economy-audit plumbing: the REAL `store` (in memory, never loaded or
saved), an in-memory `data.contracts`, and every Discord/Firestore side effect of
`contract_actions` / `api_server` muted."""
import asyncio
from _h import check, section, finish, quiet, src, between, FakeCol  # noqa: F401

import settings
import api_server
import contract_actions as ca
import rewards
from data import contracts as cdb
from data.store import store

BOT = "999"
GID = 1

DB: dict[str, dict] = {}
NOTES: list = []


def mk(cid="c1", status=cdb.PENDING, issuer="100", contractor="200",
       payment=100, fine=40, **over):
    c = {
        "contract_id": cid, "guild_id": str(GID),
        "issuer_id": str(issuer), "issuer_name": "Issuer",
        "contractor_id": str(contractor), "contractor_name": "Contractor",
        "mission": "Land on the Mun", "payment": payment, "fine": fine,
        "due_date": "2099-01-01", "status": status,
    }
    c.update(over)
    DB[cid] = c
    return c


cdb.get_contract = lambda gid, cid: dict(DB[cid]) if cid in DB else None
cdb.update_contract = lambda gid, cid, **f: DB[cid].update(f)
cdb.iter_user_contracts = lambda gid, uid: [dict(c) for c in DB.values()
                                            if str(uid) in (c["issuer_id"], c["contractor_id"])]
cdb.count_active = lambda gid, uid: sum(
    1 for c in DB.values()
    if c["status"] in (cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.MOD_REVIEW)
    and str(uid) in (c["issuer_id"], c["contractor_id"]))

_seq = [0]


def _create_contract(guild_id, issuer_id, issuer_name, contractor_id, contractor_name,
                     mission, payment, fine, due_date, **extra):
    _seq[0] += 1
    return mk(f"auto{_seq[0]}", status=cdb.PENDING, issuer=issuer_id, contractor=contractor_id,
              payment=payment, fine=fine, mission=mission, due_date=due_date, **extra)


cdb.create_contract = _create_contract

quiet(api_server)
api_server._bot_user_id = int(BOT)
api_server._get_bot_user_id = lambda: int(BOT)
api_server._bot_instance = None
api_server._create_notification = lambda *a, **k: NOTES.append(a)


async def _noop(*a, **k):
    return None


async def _false(*a, **k):
    return False

ca.restore_rescue = _noop
ca._dm_dispute_options = _noop
ca._dm_review_approved = _noop
ca._dm_settle_request = _noop
ca._dm_more_time_request = _noop
ca._escalate_to_mods = _false
ca.deliver_to_player = _noop
api_server._deliver_rescue_craft = _noop
rewards._announce_level_up = _noop


def wallet(uid, balance=0, debts=None):
    u = store.get_user(GID, uid)
    u.update({"balance": balance, "xp": 0, "level": 0, "debts": list(debts or []),
              "tx": [], "tx_totals": {}, "reward_cooldowns": {}})
    return u


def bal(uid):
    return store.get_user(GID, uid)["balance"]


def ledger_sum(uid):
    return sum(e["a"] for e in store.get_user(GID, uid).get("tx") or [])


def run(coro):
    return asyncio.run(coro)
