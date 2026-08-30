"""Rewards and channel posts that trust a field the client typed.

  A. POST /api/v1/marketplace/list pays MARKETPLACE_UPLOAD_REWARD when the
     *client-sent* `parts` list has more than MARKETPLACE_UPLOAD_REWARD_MIN_PARTS
     entries — the craft bytes are never consulted.
  B. POST /api/v1/achievement-photo with review=false posts any image to the
     community channel with no Gemini check (the check exists only on the
     review=true path).
  C. POST /api/v1/checkpoint-photo has no rate limit at all.
"""
import asyncio, io, types
from fastapi import HTTPException
from starlette.datastructures import UploadFile, Headers
from _h import check, section, finish, quiet
import settings, api_server
import cogs.screenshots as shots
from data import marketplace as mkt, craft_bans as cbans, guild_config
from data.store import store

quiet(api_server)

def upload(data=b"x", name="a.craft", ctype="text/plain"):
    return UploadFile(io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype}))

async def main():
    uid = "9401"
    u = store.get_user(0, uid); u.update({"balance": 0, "xp": 0, "reward_cooldowns": {}, "tx": [], "tx_totals": {}})
    user = {"guild_id": "0", "user_id": uid, "username": "P"}

    section("A. marketplace complexity bonus is paid on a client-typed part list")
    cbans.fingerprint = lambda b: {}
    cbans.hash_list = lambda fp: []
    async def _no_ban(*a, **k): return None
    api_server._craft_ban_refusal = _no_ban
    mkt.create_listing = lambda *a, **k: {"listing_id": "L1"}
    async def _upc(*a, **k): return "marketplace/L1/a.craft"
    mkt.upload_craft = _upc
    mkt.update_listing = lambda *a, **k: None
    parts = ",".join(f"fakePart{i}" for i in range(settings.MARKETPLACE_UPLOAD_REWARD_MIN_PARTS + 1))
    r = await api_server.marketplace_list_craft(
        craft_file=upload(b"x"), blueprint=None, thumbnail=None, craft_name="x",
        craft_type="VAB", part_count=1, mass=0.0, cost=0.0, price=1, mods="",
        parts=parts, life_support="none", ls_endurance_days=0.0, ls_crew_capacity=0,
        custom_textures="", user=user)
    check("a 1-byte 'craft' with a typed part list does not earn the complexity bonus",
          r.reward == 0, f"+{r.reward} coins for craft bytes b'x' and parts={parts[:40]}…")
    print(f"         -> {settings.MARKETPLACE_UPLOAD_REWARD} coins per account per "
          f"{settings.MARKETPLACE_UPLOAD_REWARD_COOLDOWN//3600}h, consolidated via /finance/send")

    section("B. achievement-photo review=false posts any image with no Gemini check")
    sent = []
    class Ch:
        async def send(self, *a, **k): sent.append(k)
    bot = types.SimpleNamespace(get_user=lambda i: None)
    async def _fu(i): raise RuntimeError("no")
    bot.fetch_user = _fu
    api_server._bot_instance = bot
    guild_config.resolve_channel = lambda *a, **k: Ch()
    guild_config.get_channel_id = lambda *a, **k: 1
    def _boom(*a, **k): raise AssertionError("Gemini must not be called on this path")
    shots._run_gemini = _boom
    r = await api_server.achievement_photo(photo=upload(b"\x89PNG not a ksp shot", "p.png", "image/png"),
                                           vessel_name="", body="", vessel_id="", situation="",
                                           review=False, user=user)
    check("an unreviewed image is not posted to the community channel",
          not sent, f"posted ({r.message!r}) — the 'not a KSP shot' filter lives only on the review=true path")

    section("C. checkpoint-photo has no per-user rate limit")
    settings.CHECKPOINT_PHOTOS_ENABLED = True
    sent.clear()
    for _ in range(50):
      try:
        await api_server.checkpoint_photo(photo=upload(b"\x89PNG", "c.png", "image/png"),
                                          kind="flyby", vessel_name="v", body="b",
                                          target_name="t", caption="any text", user=user)
      except HTTPException:
        pass
    check("50 checkpoint photos in a row from one account are not all posted",
          len(sent) < 50, f"{len(sent)}/50 posted, no _rate_limit call on the endpoint")
    finish()

asyncio.run(main())
