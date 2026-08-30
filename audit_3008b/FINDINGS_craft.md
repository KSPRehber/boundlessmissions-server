# Craft-in-flight audit (2026-08-30) — findings

Scope: `data/imports.py`, `data/marketplace.py`, `data/craft_bans.py`, `data/contracts.py`
storage helpers, and the `/api/v1/craft/*`, `/api/v1/gifts/*`, `/api/v1/marketplace/*`,
`/api/v1/web/marketplace/*`, contract submit/download and signed-URL paths in `api_server.py`.
PoCs: `test_craft_gift_settlement.py`, `test_craft_storage_overwrite.py`,
`test_craft_listing_lifecycle.py` (`./run_craft.sh`; everything in-memory, no Firestore/GCS/Discord).

## 1. HIGH — every quicksend offer is un-settleable: `claim_offer` NameError
- `data/imports.py:153` — `@firestore.transactional` inside `claim_offer`, but the module never
  imports `firestore` (`from data.store import _db, _storage_bucket, safe_filename, upload_private`
  is the whole import list). Both `craft_gift_accept` (`api_server.py:5068`) and `craft_gift_reject`
  (`:5114`) call it → `NameError` → HTTP 500 on every offer.
- Attacker steps: none needed; any live-vessel quicksend triggers it. `POST /craft/send` returns
  `vessel_returnable: true`, the sender's client removes the vessel from its save, and the recipient
  can neither accept nor decline. `sweep_stale_gift_files` later deletes the only copy.
- Impact: permanent loss of every live vessel quicksent since this code landed; blueprint gifts
  are stuck in the recipient's pending list forever.
- Fix: `from firebase_admin import firestore` in `data/imports.py` (as `data/marketplace.py:22`
  does). Add a test that calls `claim_offer` against a fake `_db` (the existing suites patch it out).

## 2. HIGH — rescuer overwrites the issuer's stored wreck via a filename collision
- `api_server.py:4249` (`cdb.upload_to_storage(contract_id, ss.filename, …)`) and `:4238`
  (`upload_private_to_storage(contract_id, craft_file.filename, …)`) write to
  `contracts/{cid}/{safe_filename(client name)}`; the issuer's wreck lives at the server-named
  `contracts/{cid}/rescue_vessel.cfg` (`:3657`). `safe_filename` only strips directories, so
  `rescue_vessel.cfg` is a legal client name. Screenshots are not verified as images
  (`_looks_like_image` is not used on the submit path) and `has_image` trusts the client
  content-type.
- Attacker steps: accept a rescue; submit with a "screenshot" (or craft_file) named
  `rescue_vessel.cfg` carrying an arbitrary VESSEL node. The object is replaced and — via the
  screenshot path — made public. Then give up / let the dispute fail: `_restore_issuer_vessel`
  (`:3895`) queues that same path to the issuer, whose original left their save on acceptance.
- Impact: destroy or replace another player's vessel (e.g. a 10k-part node that crashes on
  spawn, or crew with unresolvable traits — see TraitRepair); leak the private wreck publicly.
  The same collision can clobber `vessel_node.cfg` / `orbit_telemetry.png` (self-harm only).
- Fix: never let a client name choose a slot: store submissions under
  `contracts/{cid}/submitted/{uuid}_{safe_name}` (or prefix the party id), and reserve server
  names. Belt-and-braces: reject any upload whose sanitized name equals a reserved name; run
  `_looks_like_image` on screenshots; refuse upload of a name that already exists with a
  different owner (`if_generation_match=0`).

## 3. MEDIUM — `/craft/imports/{id}/done` destroys an OFFERED vessel with no return
- `api_server.py:5005-5019` — `craft_import_done` does `imp.get` + `imp.delete` with no status
  check, then `delete_gift_files(ref_id)` for `gift_*` sources.
- Attacker steps: receive a live-vessel offer (the id is in `/craft/gifts/pending`), call
  `POST /craft/imports/{id}/done` instead of reject.
- Impact: the sender's ship (already removed from their save) is deleted from Storage, no return
  entry is queued, no notification is sent. Silent remote destruction of another player's vessel.
  Also reachable by an honest client that acks the wrong id.
- Fix: in `craft_import_done`, refuse (or 409) when `entry.get("status") == "offered"` (and
  `"rejected"`); only delete gift files when the entry was `queued`.

## 4. MEDIUM — gift/import queues are keyed on the *writer's* guild, not the recipient's
- `api_server.py:5253/5261` — `imp.enqueue(gid, rid, …)` uses the sender's token guild. A
  website-only account is deliberately listed in every guild's picker (`list_corps`, `:3183`) but
  its token guild is `HOME_GUILD_ID` (`_account_guild_id`, `:5738`), and `craft_gifts_pending` /
  `craft_imports_pending` read `imp.list_pending(token gid, uid)`.
- Steps: from any non-home guild, quicksend a live vessel to a web-only player from the picker.
- Impact: the offer is written where the recipient never polls; the vessel has already left the
  sender's save; nobody can accept/decline/ack it. Same shape for contract deliveries
  (`_deliver_rescue_craft`, `_restore_issuer_vessel`, buy → `imp.enqueue(gid, uid, "market", …)`
  is fine because the buyer writes their own) whenever a web-only party is on a contract in a
  non-home guild.
- Fix: resolve the recipient's *home* queue guild once (`_account_guild_id()` for `a_…` ids,
  the corp's `guild_id` otherwise) in `imp.enqueue`, or key the queue on user only
  (`ksp_craft_imports/{uid}/items`) — the wallet already went guild-independent for this reason.

## 5. LOW — listing is ACTIVE before its craft exists
- `api_server.py:5380` (`mkt.create_listing` with `craft_url=""`, status ACTIVE) runs before
  `:5398` (`mkt.upload_craft`); a failed upload returns an error and leaves the document.
- Impact: an empty listing is on the grid; `web_marketplace_buy` (`:6491`) debits the buyer,
  credits the seller and queues an import with `craft_url=""`. Needs a Storage failure (or cost
  guard DEGRADED — which refuses uploads exactly while Firestore writes still succeed), so it is
  environmental, but it is a paid sale of nothing and the buyer cannot even re-download.
- Fix: upload first, then create the document (or create with `status="pending"` and flip to
  ACTIVE after the upload; delete the document on upload failure). `buy` should also refuse a
  listing with an empty `craft_url`.

## Minor / informational
- `data/imports.py:129` — `log.info("… for user %d", …, user_id)` raises a logging `TypeError`
  for every website id (`a_…`); the entry is still written, but every quicksend/return to a
  web account prints a traceback and the log line is lost.
- `marketplace_list_craft` caps `mods` at 100×64 chars and `parts` at 2000 entries but not the
  length of a part name; a >1 MiB document fails `create_listing` (500) rather than being
  stored — a self-DoS, not an exploit.
- The upload quota is charged *before* the ban refusal on all three paths, so a banned upload
  burns allowance. Harmless.

## Verified sound
- IDOR: import/gift ids are `uuid4().hex[:12]` (48 bits) *and* scoped under the caller's own
  `(guild, user)` path — a third party cannot accept, reject or ack another user's entry;
  `download_craft` and `get_submission_preview` check both parties; bot-issued crafts 403.
- Accept/reject racing on one offer: `claim_offer` is transactional (once the import is fixed);
  the reject path queues the return before deleting the offer; a return is only queued when the
  offer carried `vessel_pid`; `vessel_returnable` is echoed, never trusted for anything but the
  sender's own client.
- Storage paths: `safe_filename` strips directories/`..`/dot-names; marketplace and gift objects
  sit under server-minted ids (only the contract slot collision in #2 is client-influenced); the
  KSP client re-sanitizes `craft_filename` (`CraftInstaller.Install` → `safeName`).
- Ban gate order: marketplace and quicksend both fingerprint/refuse before any Storage write or
  document; submission refuses before upload; relist re-checks stored `craft_hashes`; the
  documented exemptions (rescue issue / rescue vessel node / downloads) are as described.
- Private objects: listing crafts, contract crafts/vessel nodes, gift payloads are uploaded
  without `make_public`; signed URLs are 15 min (7 days only on owner/buyer surfaces); the public
  grid never carries `craft_url`; a delisted listing cannot be bought; delist/relist/delete all
  check `seller_id`; delete with buyers degrades to a delist so purchases keep their re-download.
- Gzip: `_safe_gunzip` reads `limit+1` bytes (64 MB cap) before anything touches the payload;
  `_read_upload` caps at 25 MB; blueprints/screenshots at `MAX_BLUEPRINT_BYTES`.
- Quotas: quicksend 10/h and listing 10/h per *account*, plus the per-account daily byte
  allowance; submission 30/h.
