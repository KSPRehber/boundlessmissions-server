# Audit 3008 — fixes implemented (2026-08-30)

All fixes made in place (owner's checkout, per CLAUDE.md — no worktree). Left
UNCOMMITTED for the owner to review, since this is their working tree with WIP.
Validation: every prior-audit script + repo test passes (0 failures) except the
pre-existing `audit_2908/test_endpoint_races.py` finding, which is independent of
these changes (proven by reversing fix #4 — it still reproduces). A new
`test_audit_3008.py` regression suite (30 checks) passes 30/30.

| # | Sev | Finding | Fix | File(s) |
|---|-----|---------|-----|---------|
| 1 | HIGH | Gzip-bomb → fingerprint object explosion (13.5s/1.3GB per req) | Cap payload handed to `fingerprint()` at 4 MB; oversized → exact-hash only + `suspect`, no parse. Verified: 64 MB/4.2M-part bomb now 24 ms. | `data/craft_bans.py` |
| 2 | MED | `create_rescue` accepts non-positive payment / negative fine | `payment=Form(...,gt=0)`, `fine=Form(0,ge=0)` (matches `/contracts/create`) | `api_server.py` |
| 3 | MED | TOFU device adoption on legacy accounts (stolen token self-binds) | Empty `allowed_devices` now returns "unknown" → DM-approval flow, no silent adoption | `api_auth.py` (+ docstring, + `test_auth_hardening.py` [2c]/[3] updated to new contract) |
| 4 | MED | Submit charges upload quota AFTER fingerprint parse | Charge craft+vessel-node bytes to quota BEFORE the ban parse; screenshots charged before their own write | `api_server.py` |
| 5 | MED* | Human-contract-XP gate: lock-free read/decide + separate locked write (latent race) | New atomic `store.claim_contract_xp` (read+decide+write under one lock, mirrors `try_claim_timed_reward`); `rewards.human_contract_xp` delegates to it | `data/store.py`, `rewards.py` |
| 6 | LOW | No entropy floor on `API_SECRET_KEY` (weak key brute-forceable) | Startup guard: require length ≥ 32 when KSP API enabled | `config.py` |
| 7 | LOW | `modversion/publish` stores `download_url` unvalidated | Require `https://` scheme (422 otherwise) | `api_server.py` |
| 8 | LOW | `suspend` accepts NaN/inf hours → un-liftable suspension | `math.isfinite` guard in `suspensions.suspend` + route-level reject | `data/suspensions.py`, `api_server.py` |
| 9 | LOW | Cross-surface 2FA: KSP-link challenge redeemable at `/web/auth/totp` | Refuse a challenge carrying a link `payload` on the web side (mirror of link side) | `api_server.py` |
| 10 | LOW | Suspension doesn't close live WebSockets | `_hub.close_user()` on suspend (issue_ws_ticket already blocks reconnect) | `api_server.py` |
| 11 | LOW | Rate-limit buckets collapse if `API_TRUSTED_PROXIES` unset behind proxy | Startup warning when CORS set but no trusted proxies + strengthened `.env.example` note | `config.py`, `.env.example` |
| 12 | LOW | Avatar upload trusts content-type, no byte validation | `_looks_like_image()` check (PIL verify), 415 on failure | `api_server.py` |

\* #5 was reported HIGH by the economy agent; verified LATENT (store's lock never
yields under the critical section, so not exploitable today) — fixed anyway as
defense-in-depth so a future `await` between read and write can't reopen it.

## Not fixed (flagged for the owner)
- **Pre-existing submit-race (bot-issued auto-accept)**: `audit_2908/test_endpoint_races.py`
  reproduces "reward/XP paid more than once" under a 3× parallel submit of a
  bot-issued contract. INDEPENDENT of these changes (reversing fix #4 still
  reproduces it). The test does not fake `cdb.claim_submission` (the real atomic
  ACTIVE→SUBMITTED CAS), so it may be a test-harness artifact rather than a live
  mint — but it warrants a dedicated look, since the real guard is a Firestore
  transaction the test can't exercise against in-memory docs. Out of scope for
  this remediation; not touched.
