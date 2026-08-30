# Security audit 3008b — 2026-08-30 (consolidated)

Five surfaces audited in parallel, each with runnable in-memory PoCs (`./run.sh`;
nothing touches Firestore, GCS or Discord). 24 scripts, 20 reproduce a finding.
Per-surface detail: `FINDINGS_{account,admin,econ,ingest,craft}.md`.
Not re-covered here: everything `audit_2908*` / `test_audit_3008.py` already assert.
Transport layer (body caps incl. chunked, XFF trust, CORS, rate-limit keying) was
checked by the coordinator and is sound.

## Fix first (High)

| # | Finding | Where | PoC |
|---|---|---|---|
| H1 | **Every quicksend accept/reject 500s** — `data/imports.py` uses `@firestore.transactional` but never imports `firestore` (`NameError`). Live vessels already removed from the sender's save are stuck, then erased by `sweep_stale_gift_files`. | `data/imports.py:153` | `test_craft_gift_settlement` |
| H2 | **Rescuer overwrites the issuer's stored wreck**: submission screenshots are stored at `contracts/{cid}/{client filename}` (public, no image check) — name it `rescue_vessel.cfg` and `_restore_issuer_vessel` later hands the issuer whatever you uploaded. Also leaks the private wreck publicly. | `api_server.py:4249`, `data/contracts.py:282` | `test_craft_storage_overwrite` |
| H3 | **SSRF + unbounded read** from user-posted embed image URLs (`aiohttp.get(url)` + `resp.read()`, no host/size/timeout); bytes are forwarded to Gemini and the description posted. | `cogs/screenshots.py:307-318` | `test_ingest_images` |
| H4 | **Gemini `difficulty_rating` is an unclamped reward multiplier** — a screenshot that steers the model to `1000000` pays 18 M coins / 50 M XP (`/analyze`, achievement photo). | `cogs/screenshots.py:230,365`; `api_server.py:8745` | `test_ingest_ai_parse` |
| H5 | **Username spelled as a Discord id wins over the id**: `_USERNAME_RE` allows all-digit names and `targets.resolve` tries the username first, so `/givemoney`, `/setbalance`, `/fine`, `/setxp`, `/contractreset username:<victim id>` act on the attacker's account. | `data/accounts.py:404`, `cogs/targets.py:196` | `test_admin_targets` |

## Medium

| # | Finding | Where |
|---|---|---|
| M1 | 2FA can be silently disabled / re-seeded with no code: `begin_enroll` does `set()` without merge and the gate reads `status()`, which fails open. | `data/twofa.py:216` |
| M2 | TOTP / recovery-code anti-replay is a non-atomic read-modify-write: one code accepted 10/10 (TOTP) and 8/8 (recovery) times concurrently. | `data/twofa.py:281` |
| M3 | Four moderator commands rely only on `@default_permissions` (bypassable by any per-command permission override): `/contractreset`, `/corpsgenerate`, `/corpsprivacy`, `/ticketpanel`. | `cogs/contracts.py`, `cogs/corps.py`, `cogs/tickets.py` |
| M4 | `/setxp` / console `xp_set` with a huge value: `level_from_xp` loops ~2e9 times inside `store._lock` on the event loop — whole bot stalls. | `cogs/xp.py`, `data/store.py:set_xp` |
| M5 | `AdminUserAdjust` bare `Optional[int]`: `2**70` is accepted, Firestore encoder raises on flush, `store.save()` re-queues the whole batch forever — **no user record persists until restart**. | `api_server.py:7463`, `data/store.py:445` |
| M6 | `/contractreset` cancels + refunds outside `contract_lock`: an approve landing mid-reset pays the contractor and refunds the issuer (escrow paid twice). | `cogs/contracts.py:96-107` |
| M7 | Issuer-withdrawal fine is collected *before* the escrow refund lands → fine becomes debt while the refund is spendable. | `contract_actions.py:492-503` |
| M8 | Auction close and weekly-mission select skip every `accept` gate: `DEBT_MAX_OUTSTANDING`, `MAX_ACTIVE_CONTRACTS`, and `MAX_FINE_MULTIPLE` (judged against start price, not winning bid → 1-coin contract with a 50 000 fine). | `cogs/auctions.py:283`, `api_server.py:2533,3416` |
| M9 | `/craft/imports/{id}/done` on an *offered* vessel deletes it with no return queued (silent remote destruction of the sender's ship). | `api_server.py:5005-5019` |
| M10 | Gift/import queues keyed on the *sender's* guild; web-only recipients poll under `HOME_GUILD_ID`, so a live vessel sent from another guild is unsettleable. | `api_server.py:5253,5738` |
| M11 | Image decompression bombs: a 1 MB 13000² PNG → ~1.2 GB resident via `_shrink_image` / `flag_preview` (no pixel bound before `convert`). | `cogs/screenshots.py`, `flag_preview.py:70` |
| M12 | Rescue `mission` text uncapped; constraints re-derived on every embed on the event loop with an ~n² heuristic (5 000 chars = 3.6 s, `ValueError` at 4 300 digits) and embed fields >1024 → offer never delivered. | `api_server.py:~3490`, `cogs/contract_views.py:196`, `data/mission_constraints.py` |
| M13 | Client filename rendered verbatim in mod tickets: masked-link phishing (`click here](https://evil…) [x`) and a 1 100-char name breaks the post carrying `ModReviewView`. | `api_server.py:4242,4253` |

## Low
L1 login-challenge attempt cap non-atomic (`twofa.py:396`) · L2 suspension *lift* during a read failure caches "not suspended" for 30 s and reports success (`data/suspensions.py`) · L3 `/linkcode` mints for the raw snowflake, not the joined account (`cogs/…linkcode`) · L4 guild-tier `/overview` leaks owner-only state (DLL hash, gate switches, global counts) (`api_server.py:7514`) · L5 bot-contract `more_time` loop resets per dispute → fine stallable forever (`contract_actions.py:331`) · L6 `/setbalance` bypasses the ledger (`cogs/economy.py:317`) · L7 garnish/refund credits resurrect a deleted account (`data/store.py:956`) · L8 listing ACTIVE before its craft is uploaded; buy of an empty `craft_url` still charges (`api_server.py:5380,6491`) · L9 bug-report multipart spooled before auth; 60 MiB log buffered whole · L10 `_classify_single_contract` stores unvalidated model enums.

## Verified sound (highlights)
Account-id shape split (`a_` vs digits) everywhere except H5; link/merge flows; token epoch + audience; username claim transaction; every `/web/account/*` mutation authed. Admin tiers 404 to outsiders; every listing/announce/lock route filters by the caller's guilds; mimic is owner-gated and per-interaction. Store concurrency (400 interleaved ops conserve totals; ledgers == wallets); largest-remainder split over 3 000 random sets; self-dealing guards; refunds non-garnishable, earnings garnishable. IDOR on gifts/imports/listings/submissions; ban gate precedes every Storage write; signed URLs 15 min; `_safe_gunzip`. `i18n` format injection; `allowed_mentions`; `_ai_review_submission` verdict parsing.
