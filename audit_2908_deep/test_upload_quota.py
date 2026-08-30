"""Stored uploads have no per-user cap, so one linked account can spend the
Firebase budget (and, through the cost guard, take uploads away from everyone).

POST /api/v1/craft/send stores up to MAX_UPLOAD_BYTES per call in Storage and
calls no rate limiter. Marketplace listing and contract submission are the same
shape (FLOOD_SUBMIT only flags). Every byte counts toward
FIREBASE_MONTHLY_BUDGET_USD; at DEGRADED the guard refuses uploads for all
players, at FROZEN everything.
"""
import asyncio, io
from fastapi import HTTPException
from starlette.datastructures import UploadFile, Headers
from _h import check, section, finish, quiet
import api_server, settings
import cogs.corps as corps
from data import imports as imp

quiet(api_server)
stored = []
imp.upload_gift = lambda iid, fn, data: stored.append(len(data)) or f"gifts/{iid}/{fn}"
imp.enqueue = lambda *a, **k: {}
corps._get_corp = lambda gid, rid: {"owner_name": "Friend"}
async def _no_ban(*a, **k): return None
api_server._craft_ban_refusal = _no_ban
calls = []
_orig = api_server._rate_limit
api_server._rate_limit = lambda key, *a, **k: calls.append(key) or _orig(key, *a, **k)

async def main():
    user = {"guild_id": "0", "user_id": "9501", "username": "P"}
    section("30 quicksends in a burst from one account")
    ok = 0
    for i in range(30):
        f = UploadFile(io.BytesIO(b"x"), filename="a.craft", headers=Headers({"content-type": "text/plain"}))
        try:
            r = await api_server.craft_send_to_friend(file=f, blueprint=None, recipient_id="2",
                                                      kind="craft", craft_name="c", vessel_pid=None, user=user)
            ok += bool(r.get("success"))
        except HTTPException:
            pass
    check("quicksend applies a per-user rate limit", any(k.startswith(("send", "gift", "quick")) for k in calls),
          f"no _rate_limit call on the endpoint ({ok}/30 accepted, {len(stored)} objects stored)")
    mb = api_server.MAX_UPLOAD_BYTES / 1e6
    print(f"         -> each call may store {mb:.0f} MB (+ a blueprint); "
          f"FIREBASE_MONTHLY_BUDGET_USD=${settings.FIREBASE_MONTHLY_BUDGET_USD:.0f}")
    finish()

asyncio.run(main())
