"""
Regression checks for the 2026-08-30 endpoint-abuse audit (audit_3008/REPORT.md).

Pure-function and source-guard checks only — no Firestore, no Discord. Run with:
    python test_audit_3008.py
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")

import api_server
import api_auth
import contract_actions as ca
import settings
from cogs import perms
from fastapi import HTTPException

passed = failed = 0


def check(label, cond):
    global passed, failed
    passed += cond
    failed += (not cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")


print("\n[A1] submit_contract holds the transition lock and writes via a CAS")
src = open(os.path.join(os.path.dirname(__file__), "api_server.py")).read()
check("no private _submit_locks namespace", "_submit_locks" not in src)
check("submit body runs under ca.contract_lock", "async with ca.contract_lock(contract_id):" in src)
check("SUBMITTED is written by cdb.claim_submission, not update_contract",
      "cdb.claim_submission(gid, contract_id, update_fields)" in src
      and "cdb.update_contract(gid, contract_id, **update_fields)" not in src)


async def _lock_shared():
    order = []
    async with ca.contract_lock("c1"):
        order.append("submit")
        # A @serialized transition on the same id must wait for the submit lock.
        @ca.serialized
        async def cancel(gid, cid):
            order.append("cancel")
        t = asyncio.ensure_future(cancel(0, "c1"))
        await asyncio.sleep(0.01)
        order.append("still-submit")
    await t
    return order
check("a @serialized transition waits for the submit lock",
      asyncio.run(_lock_shared()) == ["submit", "still-submit", "cancel"])

print("\n[A2] authority is checked across every bot guild")
role_admin = SimpleNamespace(id=1)
def member(roles=(), kick=False, admin=False):
    return SimpleNamespace(get_role=lambda rid: role_admin if rid in roles else None,
                           guild_permissions=SimpleNamespace(kick_members=kick, administrator=admin))
guild_a = SimpleNamespace(id=10, get_member=lambda uid: member(roles=(1,)) if uid == 7 else None)
guild_b = SimpleNamespace(id=11, get_member=lambda uid: member() if uid in (7, 8) else None)
from data import guild_config
_orig = guild_config.resolve_role
guild_config.resolve_role = lambda g, key: role_admin if (g is guild_a and key == "admin") else None
client = SimpleNamespace(guilds=[guild_b, guild_a])
check("admin in guild A is refused even when asked from guild B", perms.holds_authority_anywhere(client, 7))
check("plain member everywhere is allowed", not perms.holds_authority_anywhere(client, 8))
check("owner id is refused", perms.holds_authority_anywhere(client, perms.cfg.OWNER_ID))
guild_config.resolve_role = _orig
acc_src = open(os.path.join(os.path.dirname(__file__), "cogs", "account.py")).read()
check("account link guard uses the cross-guild sweep", "holds_authority_anywhere" in acc_src)

print("\n[A3/A4/A7/A10] uploads are metered and throttled")
check("avatar upload charges the quota", 'avatar:{ctx' in src and "_charge_upload_quota(str(ctx[\"account_id\"]), len(data))" in src)
check("rescue wreck upload charges the quota", "_charge_upload_quota(uid, len(node_bytes))" in src)
check("rescue creation is rate limited", 'rescue:{uid}' in src)
i_charge = src.index("_charge_upload_quota(uid, len(craft_data or b\"\")")
i_craft = src.index("url = await cdb.upload_submission_file(\n                contract_id, uid, craft_file.filename")
check("submit charges the quota before storing the craft", i_charge < i_craft)
check("catalog writes are rate limited", 'catalog:{uid}' in src)

print("\n[A5] rating floor needs real participation")
settings.MARKETPLACE_AUTO_DELIST_SCORE = -20
settings.MARKETPLACE_AUTO_DELIST_MIN_VOTES = 40
listing = {"listing_id": "L", "status": "active"}
calls = []
api_server.mkt.claim_auto_delist = lambda lid, score: calls.append(lid) or False
check("20 dislikes from 20 accounts do not delist", api_server._enforce_rating_floor(listing, 0, 20) == "" and not calls)
api_server._enforce_rating_floor(listing, 15, 35)
check("the floor still engages once the votes are there", calls == ["L"])
check("vote endpoint gates new accounts",
      "_vote_eligible" in src and '_rate_limit_ip("mkvote_ip"' in src)

print("\n[A6] the web tier refuses aud-less tokens, the KSP tier still accepts them")
check("KSP tier accepts legacy", api_server._require_audience({"aud": None}, "ksp", "no", allow_legacy=True)["aud"] is None)
try:
    api_server._require_audience({"aud": None}, "web", "no", allow_legacy=False)
    check("web tier refuses legacy", False)
except HTTPException as e:
    check("web tier refuses legacy", e.status_code == 401)
try:
    api_server._require_audience({"aud": "ksp"}, "web", "no", allow_legacy=True)
    check("wrong audience is still refused", False)
except HTTPException as e:
    check("wrong audience is still refused", e.status_code == 401)
check("get_web_user passes allow_legacy=False", "allow_legacy=False" in src)

print("\n[A8] reports share one budget")
api_server._RATE_BUCKETS.clear()
req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"), headers={})
for _ in range(3):
    api_server._limit_reports("u1", 1, req)
try:
    api_server._limit_reports("u1", 1, req)
    check("fourth report in an hour is refused", False)
except HTTPException as e:
    check("fourth report in an hour is refused", e.status_code == 429)
check("both report kinds call the shared limiter", src.count("_limit_reports(uid, gid, request)") == 2
      and "mkreport:" not in src and "ctreport:" not in src)

print("\n[rate buckets] the sweep keeps hourly and daily hits")
api_server._RATE_BUCKETS.clear()
api_server._last_bucket_sweep = 0
now = time.time()
api_server._RATE_BUCKETS["k"] = [now - 1000, now - 3000]
api_server._sweep_rate_buckets(now)
check("hits within the hour survive a sweep", len(api_server._RATE_BUCKETS.get("k", [])) == 2)

print("\n[A9] abandoned gift files are swept")
from data import imports as imp
check("sweep exists", callable(getattr(imp, "sweep_stale_gift_files", None)))
check("a loop calls it", "sweep_stale_gift_files" in open(os.path.join(os.path.dirname(__file__), "cogs", "contracts.py")).read())

print("\n[A11] the Discord bid modal uses the transaction")
auc = open(os.path.join(os.path.dirname(__file__), "cogs", "auctions.py")).read()
check("BidModal goes through try_place_bid", "adb.try_place_bid" in auc and "adb.update_auction(gid, self.aid, **fields)" not in auc)

print("\n[A12] gift accept/reject claim the offer atomically")
check("claim_offer used by both handlers", src.count("imp.claim_offer") == 2 and callable(getattr(imp, "claim_offer", None)))

print("\n[A13/A14] footguns removed")
check("no either-audience onboarded dependency", not hasattr(api_server, "get_onboarded_user"))
check("purge_all_link_codes is gone", not hasattr(api_auth, "purge_all_link_codes"))
from api_models import FinanceSendRequest
try:
    FinanceSendRequest(to_user_id="x", amount=0)
    check("FinanceSendRequest refuses amount <= 0", False)
except Exception:
    check("FinanceSendRequest refuses amount <= 0", True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
