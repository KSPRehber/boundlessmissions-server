"""Marketplace listing lifecycle: what is for sale before/after the craft
object exists, and what a buyer is charged for.

Reproduced: POST /marketplace/list creates the Firestore document as ACTIVE
(with craft_url="") BEFORE the craft is uploaded, and a failed upload returns
an error without removing it. The empty listing is on the grid, and
/web/marketplace/{id}/buy debits the buyer, pays the seller and queues an
import whose craft_url is "" (sign_stored("") -> None): money for nothing.
Controls cover the ban gate order, delist/relist/delete ownership, the buy
status check, the parts/mods caps and id minting.
"""
import asyncio, io, logging
logging.disable(logging.CRITICAL)
from starlette.datastructures import UploadFile, Headers
from _h import check, section, finish, quiet, src, FakeCol
import api_server, settings
import data.store as store_mod
import data.marketplace as mkt
import data.craft_bans as cbans
from data import imports as imp
from data.store import store
from _fakes import FakeBucket

quiet(api_server)
bucket = FakeBucket()
store_mod._storage_bucket = bucket
mkt._storage_bucket = bucket
listings = FakeCol()
mkt._col = lambda: listings
api_server._charge_upload_quota = lambda uid, n: None
cbans.check = lambda data=None, fp=None: None
cbans.check_hashes = lambda entries: None
async def _no_reward(*a, **k): return (False, 0)
store.try_claim_timed_reward = _no_reward
queued = []
imp.enqueue = lambda *a, **k: queued.append((a, k)) or {}
api_server._bot_instance = None
api_server._craft_compatibility = lambda gid, uid, l: None
def _claim(gid, lid, buyer):
    d = listings.docs.get(lid)
    if d is None: return None
    if str(buyer) in d.get("buyers", []): return False
    d.setdefault("buyers", []).append(str(buyer)); d["sales_count"] = d.get("sales_count", 0) + 1
    return True
mkt.try_claim_purchase = _claim
try:
    from google.cloud.firestore_v1.transforms import Increment
except Exception:
    Increment = None

SELLER = {"guild_id": "100", "user_id": "8001", "username": "Seller"}
BUYER = {"guild_id": "100", "user_id": "8002", "username": "Buyer"}
CRAFT = b"ship = Kerbal X\nPART\n{\n part = mk1pod_1\n pos = 0,0,0\n}\n"


def up(data=CRAFT, name="kx.craft"):
    return UploadFile(io.BytesIO(data), filename=name, headers=Headers({"content-type": "text/plain"}))


async def list_craft(parts="", mods="", user=SELLER, craft=None):
    return await api_server.marketplace_list_craft(
        craft_file=craft or up(), blueprint=None, thumbnail=None, craft_name="Kerbal X",
        craft_type="VAB", part_count=1, mass=1.0, cost=1.0, price=settings.MARKETPLACE_MIN_PRICE,
        mods=mods, parts=parts, life_support="none", ls_endurance_days=0.0, ls_crew_capacity=0,
        custom_textures="", user=user)


async def main():
    section("listing goes ACTIVE before its craft exists; a buyer pays for the gap")
    real_upload = mkt.upload_craft
    async def _boom(*a, **k): raise RuntimeError("bucket down")
    mkt.upload_craft = _boom
    r = await list_craft()
    mkt.upload_craft = real_upload
    check("a failed craft upload returns an error to the seller", not r.success, r.message)
    ghost = [d for d in listings.docs.values() if d["seller_id"] == "8001"]
    check("no listing document survives a failed upload",
          not ghost, f"{len(ghost)} ACTIVE listing(s) with craft_url={ghost[0]['craft_url']!r}" if ghost else "")
    if ghost:
        lid = ghost[0]["listing_id"]
        active = [l["listing_id"] for l in listings.docs.values() if l["status"] == mkt.ACTIVE]
        check("the empty listing is not on the grid", lid not in active)
        u = store.get_user(100, "8002"); u["balance"] = 10_000
        s = store.get_user(100, "8001"); s["balance"] = 0
        rb = await api_server.web_marketplace_buy(lid, user=BUYER)
        check("buying it is refused",
              not rb.success,
              f"buy -> success={rb.success}, buyer balance {store.get_user(100,'8002')['balance']}, "
              f"seller +{store.get_user(100,'8001')['balance']}, craft_url={rb.craft_url!r}, "
              f"import queued with craft_url={queued[-1][1].get('craft_url')!r}")

    section("controls")
    listings.docs.clear(); queued.clear()
    r = await list_craft()
    check("a normal listing stores its craft privately under marketplace/{uuid}/",
          r.success and any(n.startswith("marketplace/") and not o["public"] for n, o in bucket.objects.items()))
    lid = r.listing_id
    check("listing ids are uuid4 hex[:12]", "uuid.uuid4().hex[:12]" in src("data/marketplace.py"))
    # ban gate order
    body = src("api_server.py")[src("api_server.py").index("async def marketplace_list_craft"):]
    body = body[:body.index("@app.get")]
    check("marketplace: ban check precedes create_listing and the Storage write",
          body.index("_craft_ban_refusal") < body.index("mkt.create_listing") < body.index("mkt.upload_craft"))
    qs = src("api_server.py")[src("api_server.py").index("async def craft_send_to_friend"):]
    check("quicksend: ban check precedes upload_gift and enqueue",
          qs.index("_craft_ban_refusal") < qs.index("imp.upload_gift") < qs.index("imp.enqueue"))
    # ownership
    from fastapi import HTTPException
    for fn in (api_server.marketplace_delist, api_server.web_marketplace_delist,
               api_server.web_marketplace_relist, api_server.web_marketplace_delete):
        try:
            await fn(lid, user=BUYER); ok = False
        except HTTPException as e:
            ok = e.status_code == 403
        check(f"{fn.__name__}: a non-owner is refused", ok)
    check("listing craft object survived the refused delete",
          any(n.startswith(f"marketplace/{lid}/") for n in bucket.objects))
    await api_server.web_marketplace_delist(lid, user=SELLER)
    try:
        await api_server.web_marketplace_buy(lid, user=BUYER); ok = False
    except HTTPException as e:
        ok = e.status_code == 404
    check("a delisted craft cannot be bought", ok)
    check("relist re-checks the stored craft fingerprints against the ban list",
          "cbans.check_hashes, listing.get(\"craft_hashes\")" in src("api_server.py"))
    check("public grid never carries craft_url (include_download=False by default)",
          "include_download: bool = False" in src("api_server.py"))
    check("mods capped (100 x 64 chars) and parts capped (2000 entries)",
          "[:100]" in body and "[:64]" in body and "[:2000]" in body)
    if "p.strip()[:" not in body:
        print("  note parts entries have no per-item length cap (2000 names x up to the 80 MB request "
              "cap -> a >1 MiB document fails create_listing with a 500; informational)")
    check("gzip expansion is capped before the bytes are used (decompression bomb)",
          "gz.read(limit + 1)" in src("api_server.py"))
    finish()

asyncio.run(main())
