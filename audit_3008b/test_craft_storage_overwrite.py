"""Storage object paths under contracts/{id}/ are shared by BOTH parties and by
server-named objects, and the only sanitizer (safe_filename) keeps any plain
basename. A contract submission uploads every screenshot to
contracts/{cid}/{client filename} and makes it PUBLIC, so the rescuer can name
a "screenshot" `rescue_vessel.cfg` and overwrite the issuer's stored wreck —
the very object _restore_issuer_vessel queues back to the issuer when the
rescue fails (and the object `get_active_contracts` hands the rescuer to
spawn). The issuer's original vessel was removed from their save when the
rescue was accepted; what comes back is whatever the rescuer uploaded, and it
is world-readable in the meantime. The same slot collision works with the
craft_file name (private overwrite) and against vessel_node.cfg /
orbit_telemetry.png (the contractor's own, harmless).

Reproduced by driving _submit_contract_locked against an in-memory bucket.
"""
import asyncio, io, logging
logging.disable(logging.CRITICAL)
from starlette.datastructures import UploadFile, Headers
from _h import check, section, finish, quiet, src
from _fakes import FakeBucket
import api_server
import data.store as store_mod
import data.contracts as cdb
from data import imports as imp

quiet(api_server)
bucket = FakeBucket()
store_mod._storage_bucket = bucket
cdb._storage_bucket = bucket
api_server._charge_upload_quota = lambda uid, n: None
async def _no_ban(*a, **k): return None
api_server._craft_ban_refusal = _no_ban
api_server._validate_rescue_submission = lambda *a, **k: (True, "")
api_server._get_bot_user_id = lambda: "1"

CID = "abc123def456"
ISSUER, RESCUER = "7001", "7002"
WRECK = b"issuer-wreck-node-gzip-bytes"
contract = {
    "contract_id": CID, "issuer_id": ISSUER, "contractor_id": RESCUER,
    "issuer_name": "Issuer", "contractor_name": "Rescuer",
    "status": cdb.ACTIVE, "mission_type": cdb.RESCUE, "mission": "Rescue Bob",
    "constraints": None, "modlist": None, "issuer_vessel_removed": True,
    "rescue_vessel_node_url": f"contracts/{CID}/rescue_vessel.cfg",
}
cdb.get_contract = lambda gid, cid: dict(contract) if cid == CID else None
updates = {}
cdb.update_contract = lambda gid, cid, **f: updates.update(f)
def _claim(gid, cid, fields):
    contract.update(fields); return True
cdb.claim_submission = _claim
queued = []
imp.enqueue = lambda *a, **k: queued.append((a, k)) or {}


def up(name, data, ct):
    return UploadFile(io.BytesIO(data), filename=name, headers=Headers({"content-type": ct}))


async def submit(**files):
    return await api_server._submit_contract_locked(
        CID, craft_file=files.get("craft"), vessel_node=None, loadmeta=None,
        vessel_data=None, screenshot1=files.get("shot"), screenshot2=None, screenshot3=None,
        screenshots=[], modlist=None, used_modlist=None, used_parts=None, delta_v_vac=None,
        life_support=None, ls_endurance_days=0.0, ls_crew_capacity=0, cheat_report=None,
        user={"guild_id": "100", "user_id": RESCUER, "username": "Rescuer"})


async def main():
    section("rescuer overwrites the issuer's stored wreck through a screenshot name")
    # The issuer's wreck, stored private at rescue creation (api_server.py:3657).
    await cdb.upload_private_to_storage(CID, "rescue_vessel.cfg", WRECK, "application/gzip")
    obj = bucket.objects[f"contracts/{CID}/rescue_vessel.cfg"]
    check("wreck is stored private under a server-chosen name", obj["data"] == WRECK and not obj["public"])

    junk = b"VESSEL{ name = 9000-part kraken } "
    r = await submit(shot=up("rescue_vessel.cfg", junk, "image/png"),
                     craft=up("lander.craft", b"ship = x", "text/plain"))
    check("submission with a screenshot named rescue_vessel.cfg is accepted",
          r.success, r.message)
    obj = bucket.objects.get(f"contracts/{CID}/rescue_vessel.cfg")
    check("the issuer's wreck object still holds the issuer's bytes",
          obj is not None and obj["data"] == WRECK,
          f"contracts/{CID}/rescue_vessel.cfg now = {obj['data'][:40]!r} (from the rescuer's 'screenshot')")
    check("the wreck object is still private",
          obj is not None and not obj["public"],
          "upload_to_storage() made it public — anyone with the URL reads it")

    # What the issuer gets back when the rescue then fails (give up / fine / dispute).
    c = cdb.get_contract(100, CID)
    await api_server._restore_issuer_vessel(100, CID, c)
    path = queued[-1][1].get("vessel_node_url") if queued else None
    check("the restore queues the issuer's own wreck",
          path and bucket.objects.get(path, {}).get("data") == WRECK,
          f"issuer is queued {path!r} whose bytes are the rescuer's upload")

    section("same slot, private overwrite via the craft_file name")
    bucket.objects[f"contracts/{CID}/rescue_vessel.cfg"] = {"data": WRECK, "ct": "application/gzip", "public": False}
    contract["status"] = cdb.ACTIVE
    r = await submit(shot=up("s.png", b"\x89PNG", "image/png"),
                     craft=up("rescue_vessel.cfg", b"ship = attacker", "text/plain"))
    obj = bucket.objects[f"contracts/{CID}/rescue_vessel.cfg"]
    check("craft_file cannot be stored over the wreck slot",
          obj["data"] == WRECK, f"slot now = {obj['data']!r}")

    section("controls")
    check("safe_filename strips directory components and dot-names",
          store_mod.safe_filename("../../x/../evil.cfg") == "evil.cfg"
          and store_mod.safe_filename("..") == "file"
          and "/" not in store_mod.safe_filename("a/b\\c"))
    check("marketplace/gift objects live under server-minted uuid ids",
          'path = f"marketplace/{listing_id}/' in src("data/marketplace.py")
          and 'path = f"gifts/{import_id}/' in src("data/imports.py"))
    check("screenshot bytes are never verified as images on submit",
          "_looks_like_image" not in src("api_server.py")[src("api_server.py").index("async def _submit_contract_locked"):
                                                            src("api_server.py").index("async def _ai_review_submission")],
          "(informational: the has_image gate trusts the client content_type)")
    finish()

asyncio.run(main())
