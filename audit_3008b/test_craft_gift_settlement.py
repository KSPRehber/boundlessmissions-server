"""Friend quicksend: the hand-over of a LIVE vessel is only safe if the offer can
be settled (accept/reject) and only the settlement paths can retire the files.

Reproduced here:
  1. data/imports.claim_offer references `firestore` without importing it, so
     POST /craft/gifts/{id}/accept and /reject raise NameError (HTTP 500) for
     every offer. The sender's client has already removed the vessel from their
     save (vessel_returnable=True), so a quicksent live vessel can be neither
     accepted nor returned; sweep_stale_gift_files erases it for good later.
  2. POST /craft/imports/{id}/done deletes an entry regardless of status. A
     recipient can "ack" an OFFERED gift_vessel: the offer and the Storage
     files are gone, nothing is returned to the sender, and the sender is never
     told. That is a remote, silent destruction of another player's ship.
  3. The gift queue is keyed on the SENDER's token guild, but a website-only
     recipient (listed in every guild's picker by design) polls under
     HOME_GUILD_ID. Sent from any other guild, the offer is written where the
     recipient will never look — and for a vessel, the ship is already gone
     from the sender's save.
Controls: import ids are 48-bit uuid4 fragments scoped under the caller's own
(guild, user) path, so nobody can ack/accept/reject someone else's entry.
"""
import asyncio, io, uuid
from starlette.datastructures import UploadFile, Headers
from _h import check, section, finish, quiet, src
from _fakes import FakeBucket, FakeQueues
import logging
logging.disable(logging.CRITICAL)
import api_server
import data.store as store_mod
import cogs.corps as corps
from data import imports as imp
from config import cfg

quiet(api_server)
bucket = FakeBucket()
imp._storage_bucket = bucket
store_mod._storage_bucket = bucket
queues = FakeQueues()
imp._col = queues
class _DB:
    def transaction(self): return object()
imp._db = _DB()
corps._get_corp = lambda gid, rid: {"owner_name": "Friend", "guild_id": str(gid)}
async def _no_ban(*a, **k): return None
api_server._craft_ban_refusal = _no_ban
api_server._charge_upload_quota = lambda uid, n: None
notes = []
api_server._create_notification = lambda gid, uid, kind, *a, **k: notes.append((str(uid), kind))

SENDER = {"guild_id": "100", "user_id": "9001", "username": "Sender"}
RECIP = {"guild_id": "100", "user_id": "9002", "username": "Recip"}
THIRD = {"guild_id": "100", "user_id": "9003", "username": "Third"}


def upload(name="ship.cfg"):
    return UploadFile(io.BytesIO(b"VESSEL{ pid = abc }"), filename=name,
                      headers=Headers({"content-type": "text/plain"}))


async def send_vessel(sender=SENDER, rid="9002"):
    r = await api_server.craft_send_to_friend(
        file=upload(), blueprint=None, recipient_id=rid, kind="vessel",
        craft_name="Mun Lander", vessel_pid="12345", user=sender)
    return r


async def main():
    section("1. accept/reject of any offer crashes (imports.claim_offer NameError)")
    r = await send_vessel()
    check("quicksend of a live vessel is accepted and promises a return",
          r.get("success") and r.get("vessel_returnable") is True, str(r))
    offer = queues.entries("100", "9002")[0]
    iid = offer["import_id"]
    err = None
    try:
        await api_server.craft_gift_accept(iid, user=RECIP)
    except NameError as exc:
        err = exc
    check("recipient can accept the offer",
          err is None, f"craft_gift_accept raised {type(err).__name__}: {err}")
    err = None
    try:
        await api_server.craft_gift_reject(iid, user=RECIP)
    except NameError as exc:
        err = exc
    check("recipient can decline the offer (which returns the vessel)",
          err is None, f"craft_gift_reject raised {type(err).__name__}: {err}")
    check("data/imports.py imports firestore for claim_offer",
          "from firebase_admin import firestore" in src("data/imports.py")
          or "import firestore" in src("data/imports.py"),
          "`@firestore.transactional` is used at data/imports.py:153 but the name is never imported")
    still_offered = queues("100", "9002").docs[iid]["status"] == "offered"
    print(f"         -> offer {iid} is still 'offered' ({still_offered}); the sender's "
          f"save no longer has the ship; {len([n for n in bucket.objects if n.startswith('gifts/')])} "
          "gift object(s) wait for sweep_stale_gift_files to erase them")

    # From here on, give the module the name it is missing, so the rest of the
    # settlement flow can be exercised as designed.
    from firebase_admin import firestore as _fs
    class _Txn:
        def __init__(self, ref): self.updates = []
        def update(self, ref, data): ref.update(data)
    def _claim_offer(gid, uid, import_id, new_status):
        ref = imp._col(gid, uid).document(import_id)
        snap = ref.get()
        if not snap.exists: return None
        d = snap.to_dict()
        if (d.get("status") or "queued") != "offered": return None
        ref.update({"status": new_status})
        return d
    imp.claim_offer = _claim_offer

    section("2. /imports/{id}/done on an OFFERED vessel destroys it without a return")
    bucket.objects.clear(); notes.clear()
    r = await send_vessel()
    assert r.get("success"), r
    offer = [e for e in queues.entries("100", "9002") if e["status"] == "offered"][-1]
    iid, ref_id = offer["import_id"], offer["ref_id"]
    before = [n for n in bucket.objects if n.startswith(f"gifts/{ref_id}/")]
    r = await api_server.craft_import_done(iid, user=RECIP)
    after = [n for n in bucket.objects if n.startswith(f"gifts/{ref_id}/")]
    returned = queues.entries("100", "9001")
    check("done() refuses an entry that is still an offer",
          not r.get("success") and len(after) == len(before),
          f"done -> {r}; gift files {before} -> {after}; return entries queued to sender: {len(returned)}; "
          f"sender notified: {[n for n in notes if n[0]=='9001']}")

    section("3. gift written under the sender's guild, invisible to a web-only recipient")
    home = str(cfg.HOME_GUILD_ID or 0)
    other = "200" if home != "200" else "201"
    sender_other = {"guild_id": other, "user_id": "9001", "username": "Sender"}
    web_recip = {"guild_id": home, "user_id": "a_web1", "username": "Web"}
    bucket.objects.clear()
    r = await send_vessel(sender=sender_other, rid="a_web1")
    check("send to a web-only account from a non-home guild is accepted",
          r.get("success") and r.get("vessel_returnable"), str(r))
    seen = await api_server.craft_gifts_pending(user=web_recip)
    written_under = [g for (g, u), c in queues.cols.items() if u == "a_web1" and c.docs]
    check("the recipient's pending poll (their own token guild) shows the offer",
          len(seen["gifts"]) == 1,
          f"offer written under guild {written_under}, recipient polls guild {home}: "
          f"{len(seen['gifts'])} gift(s) visible — the vessel already left the sender's save")

    section("controls")
    bucket.objects.clear()
    await send_vessel()
    iid = queues.entries("100", "9002")[0]["import_id"]
    r1 = await api_server.craft_gift_accept(iid, user=THIRD)
    r2 = await api_server.craft_import_done(iid, user=THIRD)
    check("a third party cannot accept or ack another user's offer by id",
          not r1.get("success") and not r2.get("success"))
    check("import ids are unguessable (uuid4 hex[:12], 48 bits)",
          "uuid.uuid4().hex[:12]" in src("data/imports.py"))
    r_acc = await api_server.craft_gift_accept(iid, user=RECIP)
    r_rej = await api_server.craft_gift_reject(iid, user=RECIP)
    check("accept then reject: the second settlement is refused",
          r_acc.get("success") and not r_rej.get("success"))
    check("a returned vessel is only queued when the offer carried vessel_pid",
          "entry.get(\"vessel_pid\")" in src("api_server.py"))
    finish()

asyncio.run(main())
