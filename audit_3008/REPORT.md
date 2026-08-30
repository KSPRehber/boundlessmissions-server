# Security Audit 3008 — Server / endpoint deep inspection
Date: 2026-08-30 · Method: 4 parallel deep-inspection subagents (auth, economy,
admin, craft/input) + a cross-cutting infra pass, each attempting concrete
endpoint manipulation. Findings below are RANKED and carry a verification verdict
from the lead (me) — where an agent's grade changed on inspection, it is stated.

## Headline
The server is well-defended. The auth spine, the owner/admin privilege boundary,
and the money-transition state machine are all soundly implemented — no live auth
bypass, token forgery, cross-audience escalation, IDOR, privilege escalation, or
economic mint was confirmed. The real, live issue is a **denial-of-service** in
the craft-fingerprint parser. Everything else is input-validation hardening or
latent/defense-in-depth fragility.

---

## 1. HIGH — Gzip-bomb amplification in the craft-fingerprint scanner (LIVE, verified)
`data/craft_bans.py` `fingerprint`/`_parts_of`/`_preformat`, reached from
marketplace-list (`api_server.py:5350`), quicksend (`:5219`) and submit (`:4021`).
A ~127 KB gzip upload expanding to 64 MB of minimal `PART{}` blocks passes every
byte gate — Content-Length 80 MB, `_read_upload` 25 MB (127 KB on the wire),
`_safe_gunzip` 64 MB decompress cap — but the scanner then turns that text into
~4.2M Python tuples plus a multimillion-element `sorted()` (verified: no
part-count ceiling or early bail at craft_bans.py:152, 184, 303-304).
Agent measurement: one request → ~13.5 s CPU, ~1.3 GB peak RSS. The API is
in-process with the Discord bot, so a few concurrent requests OOM a small host.
The decompress cap bounds *bytes*, not the *object graph* built from them.
FIX: cap the craft text handed to the fingerprint far below 64 MB (a real .craft
is < 2 MB) and/or bail past a part-count ceiling. See also #4.

## 2. MEDIUM — `/contracts/create_rescue` accepts non-positive payment / negative fine (LIVE, verified)
`api_server.py:3463-3464`: `payment: int = Form(...)`, `fine: int = Form(0)` —
the only money params in the codebase without `gt=0`/`ge=0`. Handler body
(`:3468+`) validates contractor, self-contract and due-date but NOT the amounts.
A `payment<=0` passes the balance check, `try_debit` no-ops (no escrow), and on
approval a negative payment *debits the contractor* (clamped) and destroys coins.
Griefing / ledger-integrity, not a mint. FIX: `payment=Form(...,gt=0)`,
`fine=Form(0,ge=0)`.

## 3. MEDIUM — Trust-on-first-use device adoption for pre-device-binding sessions (verified, precondition-gated)
`api_auth.py:445-466`. An account whose `allowed_devices` is legacy/empty can have
an attacker's device silently bound as trusted with NO Discord DM approval — but
only if the attacker ALREADY holds a stolen KSP token for it (new links bind the
linker's own device). Blast radius limited by the stolen-token precondition.
FIX: require explicit approval for the first device even on legacy accounts, or
seed `allowed_devices` at link time.

## 4. MEDIUM — Submit charges upload quota AFTER the fingerprint parse (LIVE, verified; compounds #1)
`submit_contract` runs `_craft_ban_refusal` (fingerprint) at `api_server.py:4021`
but `_charge_upload_quota` only at `:4223`. Quicksend/marketplace charge quota
BEFORE the parse (capping bombs at ~4-5/day/account); submit is bounded only by
the 30/hr rate limit — this is what makes #1 reachable 30×/hr on submit instead
of ~4×/day. FIX: charge quota before the ban check on the submit path too.

## 5. MEDIUM (was reported HIGH) — Human-contract-XP anti-farm gate is a lock-free read/decide + locked write (LATENT, NOT live)
`rewards.human_contract_xp` reads `store.last_contract_xp_at`/`contract_xp_log`
lock-free (rewards.py:109,113), decides, then writes under `store._lock` in
`note_contract_completion` (store.py:566). `review` is serialized PER-CONTRACT
(contract_actions.py:160), so two contracts between a colluding pair run
concurrently — the shape of a TOCTOU XP/level-up-coin farm.
LEAD VERIFICATION — DOWNGRADED FROM HIGH: I traced `add_balance_gross`
(store.py:1005) and `note_contract_completion` (store.py:566): both do ALL work
synchronously inside `async with self._lock` with NO `await` in the body, and
add_balance touches only the in-memory buffer (no IO). So the lock never yields
while held; a second `review` coroutine can never queue on it mid-critical-
section; the lock-free read and the locked write are therefore ATOMIC w.r.t. other
coroutines. The race is NOT exploitable in the current code — exactly what the
authors assert at contract_actions.py:120-127. It is genuine latent fragility:
it becomes a live HIGH the instant anyone adds a real `await` (a to_thread'd
Firestore call, a Discord DM) between the read and the write. FIX (still worth
doing): fold read-decide-note into one `store._lock` critical section
(`claim_contract_xp`), mirroring `try_claim_timed_reward`.

## 6. LOW — No length/entropy floor on `API_SECRET_KEY` (verified; found independently by 2 agents)
`config.py:158`. Startup rejects a placeholder/blank secret but accepts ANY other
value, e.g. `API_SECRET_KEY=x`. The token payload is base64-visible, so a weak
HMAC key is offline-brute-forceable from any one token → universal forgery.
FIX: require `len >= 32` (or an entropy check) in the same startup guard.

## 7. LOW — `modversion/publish` stores `download_url` verbatim (owner-only)
`data/mod_version.py` / admin publish route. No https/host validation, so a
compromised OWNER session could point every client's update prompt anywhere. Not
reachable by a guild-admin. FIX: validate scheme/host allowlist.

## 8. LOW — `suspend` accepts `NaN` hours (owner-only robustness)
Admin suspend route + `suspensions.suspend`. NaN passes the bounds check (all NaN
comparisons are False) → `until = now + nan`. Owner-only, integrity not boundary.
FIX: reject non-finite durations (`math.isfinite`).

## 9. LOW — Cross-surface 2FA-challenge redemption (verified, no escalation)
`api_server.py:5867` vs `1013`. A KSP-link TOTP challenge can be completed at
`/web/auth/totp` for a WEB token. Needs the real TOTP code + same account, and
only in the harmless lower-privilege direction (the web→KSP direction is
correctly blocked). FIX: bind a challenge to the surface that created it.

## 10. LOW — Suspension does not close live WebSockets
`api_server.py:4693`. A socket opened before a suspension keeps streaming
notifications; only `logout_all` closes sockets. FIX: drop the user's live
sockets when a suspension is issued.

## 11. LOW — Rate-limit buckets go global if `API_TRUSTED_PROXIES` unset behind a proxy
`api_server.py:593-622`. Self-DoS risk (every request shares one bucket), NOT a
spoofing bypass — XFF is correctly ignored from untrusted peers. Deployment-config
concern; document that `API_TRUSTED_PROXIES` must be set behind Caddy.

## 12. LOW — Avatar upload skips image validation
`web_account_avatar` (`api_server.py:5963`) trusts the claimed content-type,
never runs `_looks_like_image`. Low impact (private object, signed URL, inert
served type), inconsistent with the photo paths. FIX: validate magic bytes.

## INFORMATIONAL / by-design (no action, noted)
- Guild admins see bot-wide aggregate COUNTS in the admin overview (member lists
  cut; documented read-only facts).
- `craft_bans.fingerprint` sets `suspect=True` on a PART-bearing payload that
  yields no parts, but nothing enforces it — matches the module's documented "a
  text edit defeats any hash" stance. Consider ticketing on `suspect=True`.
- Documented fail-open reads (token-version, allowed-devices, suspension, craft-
  ban list) are intentional and correctly implemented; `twofa.is_enabled`
  correctly fails CLOSED.
- `KSP_2FA_ENABLED=false` turns a brute-forced link code directly into a token;
  default is true and brute-force is impractical (~0.2% per code lifetime).

## Confirmed-solid (attacked, held) — abbreviated
Auth: HMAC compare_digest, aud separation, tv revocation, TOTP replay caps,
device/login approvals server-checked. Economy: `/finance/send`, buy (self-buy
blocked, transactional claim + refund-loser), vote race (now stripe-locked),
delist/relist/delete ownership, auctions (transactional bid, single-refund
close), all contract transitions (party+status checks, actor_id = authenticated
user, garnishment bounded, no negative debt). Admin: uniform 404, per-resource
guild scoping via `_admin_can_guild`, `/web/game/command` allow-listed +
ownership-checked, cheat gate not console-flippable. Craft: `safe_filename`
path-traversal block, server-generated storage paths, signed URLs only over
stored paths, IDOR gated on both download endpoints + per-uid import/gift queues,
gift accept/reject transactional, no Discord ping-injection (`everyone=True`
never set on user text). Infra: no wildcard CORS, docs gated, dual body-size
guards (Content-Length + chunked), no RCE/deser sinks, SSRF closed
(download_url only sees server-generated storage paths), bounded rate-limiter.

## Detail files
audit_3008/auth_session.md · economy.md · admin_console.md · craft_input.md · infra_crosscutting.md
