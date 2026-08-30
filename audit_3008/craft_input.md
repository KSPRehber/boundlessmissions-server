# Audit 3008 — Craft / upload / untrusted-input surface

Scope: craft/upload/file endpoints in `api_server.py` and the parse/store/serve
chain behind them (`data/craft_bans.py`, `data/imports.py`, `data/tickets.py`,
`data/telemetry_check.py`, `data/cheat_check.py`, `data/suspicion.py`, Firebase
Storage helpers in `data/store.py` / `data/contracts.py` / `data/marketplace.py`).

Method: traced each upload/download from request bytes -> parse/store/serve. Read
the prior-audit tests first (`audit_2908/test_craft_ban_fingerprint.py`,
`audit_2908_deep/test_upload_quota.py`, `test_client_attested_rewards.py`,
`test_gemini_review_surface.py`) to avoid duplicating already-fixed findings.

Bottom line: the surface is heavily hardened and most classic hazards are already
closed (see "What is already solid"). One genuinely exploitable NEW finding: a
gzip-bomb that amplifies inside the craft-fingerprint scanner to ~1.3 GB RAM +
~13 s CPU per single ~127 KB request, in the process shared with the Discord bot.
Everything else is contributing-factor or informational.

---

## 1. Gzip-bomb amplification in the craft-fingerprint scanner (memory-exhaustion DoS)

- Severity: HIGH (single small authed request can OOM the shared bot process)
- Status: CONFIRMED (measured)
- Where:
  - data/craft_bans.py:107 _parts_of / :161 _preformat / :250 fingerprint (the amplifier)
  - api_server.py:313 _safe_gunzip (64 MB decompress cap - the only gate before the scanner)
  - Reached from: api_server.py:5350 (marketplace list), :5219 (quicksend),
    :4021 (contract submit, via _craft_ban_refusal -> _craft_text_bytes -> _safe_gunzip)

### Hostile input
A gzip payload of ~127 KB whose decompressed content is 64 MB of minimal
ConfigNode parts:  "PART\n{\npart=x\n}\n" repeated ~4.2 million times (67 MB,
gzips to 127 KB, 515x ratio). POST it as the craft on any craft-accepting
endpoint (e.g. POST /api/v1/contracts/{id}/submit with craft_file=<gz>; also
/api/v1/marketplace/list, /api/v1/craft/send). The 127 KB body passes the
Content-Length middleware (MAX_REQUEST_BYTES 80 MB), passes _read_upload
(MAX_UPLOAD_BYTES 25 MB - it is only 127 KB on the wire), and _safe_gunzip
expands it to 64 MB (its cap MAX_DECOMPRESSED_BYTES = 64 MB).

### Effect (measured)
```
payload 67.1MB compresses to 127.3KB (ratio 515x)
gunzip+fingerprint: 13.48s CPU, parts=4,194,304, peak RSS = 1266 MB
```
The 64 MB text is turned by _parts_of/_preformat into ~4.2 M Python tuples plus a
4.2 M-element string sort for the design hash - ~1.3 GB of transient objects and
~13 s CPU for ONE request. The scanner is linear (verified 1-32 MB: ~0.1-0.2 s/MB),
so it is amplification, not quadratic blow-up - but the 64 MB decompress cap sizes
the amplification at 64 MB -> ~1.3 GB.

Because the API runs in-process with the Discord bot, one OOM kills both. On a
typical 2-4 GB VPS a single request risks OOM; a few concurrent requests guarantee
it. The fingerprint runs in asyncio.to_thread, so N concurrent requests hold
N x 1.3 GB simultaneously (default pool up to min(32, cpu+4) workers).

### Why the existing mitigations don't catch it
- _read_upload caps the compressed wire bytes (127 KB), not the decompressed text.
- _safe_gunzip caps decompressed size at 64 MB - but 64 MB of ConfigNode text is
  already enough to amplify to >1 GB in the scanner. The cap bounds the bytes, not
  the object graph the parser builds from them.
- The per-user upload quota does NOT bound this on the worst path. In
  submit_contract, _craft_ban_refusal (which runs the fingerprint) is called at
  api_server.py:4021, but _charge_upload_quota is only called at :4223 - AFTER the
  parse. So on the submit path the expensive parse happens with no quota check at
  all, bounded only by the submit:{uid} rate limit of 30/hour. (Marketplace and
  quicksend charge quota before the parse, capping at ~4-5 bombs/day/account via the
  300 MB/day budget - still enough to hurt, but submit is the free path.)

### Fix
Cap the craft text handed to the fingerprint far below 64 MB - a real .craft is
under a couple of MB. Either add a dedicated MAX_CRAFT_TEXT_BYTES (e.g. 8 MB) and
truncate/refuse in _craft_text_bytes before fingerprint; and/or bail the scanner
out of _parts_of/_preformat past a part/line ceiling (e.g. stop after ~50 k parts
and mark suspect=True); and move _charge_upload_quota in submit_contract above the
ban check so the parse is inside the quota, matching quicksend/marketplace.

---

## 2. Submit charges the upload quota AFTER the ban-fingerprint parse

- Severity: MEDIUM (contributing factor to #1; also lets an over-quota user still spend the parse)
- Status: CONFIRMED (code ordering)
- Where: api_server.py:4021 (_craft_ban_refusal) vs api_server.py:4223 (_charge_upload_quota)

On the submit path the daily upload budget is charged only when the storage writes
are about to happen, which is after the craft and vessel node have already been
decompressed (:4005-4006) and fingerprinted. Quicksend (:5215 before :5219) and
marketplace (:5345 before :5350) order it the other way. The consequence is that
the most CPU/RAM-intensive step on the submit path - the fingerprint scan - is
outside the one mechanism meant to bound per-account resource use, which is exactly
what makes #1 reachable 30x/hour rather than ~4x/day. Reorder to charge quota (on
the decompressed sizes) before the ban check.

---

## 3. `suspect` fingerprint flag is computed but never enforced (residual ban-evasion)

- Severity: INFORMATIONAL (matches the module's documented "nuisance control" design)
- Status: CONFIRMED
- Where: data/craft_bans.py:295-299 sets fp["suspect"]; no consumer anywhere
  (grep suspect -> only the two set sites)

When a payload contains PART node tokens but the scanner reads zero parts
(design/parts = None), fingerprint sets suspect=True and logs a warning - but no
caller reads the flag. craft_bans.check then matches only on exact, so a craft the
moderator design/parts-banned slips through if an attacker can keep it KSP-loadable
while making the scanner yield no parts. The prior test already showed two cheap
fingerprint-only mutations that KSP loads (same-line brace - now handled by
_preformat; a stray name= ahead of part= - handled by "part wins"), so the scanner
tracks KSP's loader closely. craft_bans.py's own docstring concedes a text edit
defeats any hash and that this is nuisance control, not a security boundary - so
this is a noted residual, not a new bug. Worth considering: have a caller open a
suspicion ticket (data/suspicion.py) on suspect=True for a craft being
listed/sent/submitted, so an unparseable-but-loadable upload is surfaced to a
moderator instead of only appearing in a log line.

---

## 4. Avatar upload stores bytes without verifying they are an image

- Severity: LOW
- Status: CONFIRMED
- Where: api_server.py:5963 web_account_avatar - validates content_type against
  _AVATAR_TYPES but never calls _looks_like_image(data) before upload_private

Only the claimed content type is checked; the bytes are stored as-is under
avatars/{account_id}. Impact is limited: the object is private, served via a signed
URL from the Firebase Storage origin (not the app origin), and safe_content_type
clamps the served type to the inert allowlist, so it can't be served as active
content. The stored value is later used as a Discord embed icon_url (ticket author
line), where non-image bytes simply fail to render. Add a _looks_like_image check
for consistency with the checkpoint/achievement paths.

---

## What is already solid (verified, no finding - several are prior-audit fixes now landed)

- Upload size / request caps: Content-Length middleware + _BodyCapMiddleware
  (chunked-aware) at MAX_REQUEST_BYTES 80 MB; _read_upload hard-caps each file
  (25 MB, 60 MB logs, MAX_BLUEPRINT_BYTES for renders).
- Decompression: every craft/log/vessel-node gunzip goes through _safe_gunzip
  (reads limit+1, 413 past 64 MB) - no raw gzip.decompress on untrusted input
  anywhere in scope (checked quicksend, marketplace, submit, _extract_crew_names,
  _extract_part_uids, device/bug logs).
- Path traversal: safe_filename strips directory components, .., leading dots;
  storage paths are contracts/{id}/, marketplace/{id}/, gifts/{id}/,
  avatars/{account_id} where the id is server-generated or the token's own account -
  no client-controlled path segment reaches a blob path. FastAPI {contract_id} path
  params can't contain /.
- Signed URLs are minted only over server-stored paths (sign_stored on
  contract/import/gift fields), never over an attacker-supplied path.
- IDOR: download_craft and get_submission_preview gate on uid in
  {issuer_id, contractor_id} and 403 otherwise; bot-issued craft download refused.
  imports.py keys every entry under guilds/{gid}/.../{uid}/..., so craft/imports/*
  and craft/gifts/* (imp.get/claim_offer/delete) are scoped to the token's own uid -
  a cross-user import_id simply isn't found. device_report checks the uploader is the
  reported account.
- Gift/quicksend hand-over: claim_offer is a Firestore transaction
  (offered -> queued/rejected), so a racing accept+reject settle once; the
  decline-return is queued before the offer is deleted and only returns entries
  carrying vessel_pid - no dupe/loss path found.
- Ban fingerprint correctness: prior-audit mutations (rename, re-description, fresh
  instance ids, sub-cm float noise, CRLF, same-line brace, stray name=,
  part=-no-space, VESSEL-vs-.craft dialect, truncation, empty) all handled. Brace
  depth is an integer counter (no recursion -> no stack overflow); scanner is linear.
- Content injection into Discord: ticket/bug-report/report/checkpoint text all go
  into embed descriptions (no ping from embeds); create_ticket builds the ping
  content only from server-side opener.mention + role mentions and uses
  AllowedMentions(roles=True, users=True) (no everyone=True) - @everyone/@here in
  user text can't ping. Report subject / target guild are derived from the
  contract/listing, not client-chosen; ticket kind is allow-listed and guild is
  HOME_GUILD_ID.
- Cheat/telemetry/AI trust (prior-audit fixes confirmed landed): AI review no longer
  reads ksp_level from the model (_auto_accept_contract(..., 0)); client text is
  fenced in _client_text_block and the model is told it's untrusted data;
  Gemini-unavailable now holds for a moderator (_hold_for_mod_review) instead of
  auto-accepting; AI images capped MAX_AI_IMAGES, submission images capped
  MAX_SUBMISSION_IMAGES; cheat_check.evaluate and telemetry_check treat a
  malformed/absent report as a clean no-op and never raise.
- Prior quota/reward gaps closed: per-user _charge_upload_quota (300 MB/day) now
  exists; marketplace complexity bonus judged on craft_fp["distinct_parts"] (parsed
  bytes) not the client parts field; checkpoint-photo now rate-limited (5/hr) and
  image-validated; achievement review=false now _looks_like_image + rate-limited.
- Bug report always runs _trim_log (2 MB head + 7 MB tail) after a 60 MB read.

---

## Reproduction artifact (confirms finding #1)

Run in the project venv:
```
.venv/bin/python -c '
import gzip; from data import craft_bans as cb; from api_server import _safe_gunzip
one=b"PART\n{\npart=x\n}\n"; payload=one*((64*1024*1024)//len(one))
comp=gzip.compress(payload)                       # ~127 KB
out=_safe_gunzip(comp); fp=cb.fingerprint(out)    # ~13.5 s, ~1.3 GB RSS, 4.2 M parts
'
```
