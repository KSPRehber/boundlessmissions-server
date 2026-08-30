# Account system & web authentication — security audit (2026-08-30)

Surface: `data/accounts.py`, `data/twofa.py`, `cogs/account.py`, `api_auth.py`,
and the `/api/v1/{auth,web/auth,web/account}/*` endpoints in `api_server.py`
(+ `api_models.py`). PoCs in `audit_3008b/test_account_*.py`; run `bash
audit_3008b/run.sh` (`.venv/bin/python`). Nothing writes to Firestore/Discord —
all state is in-memory doubles.

There is **no local password store**: Firebase owns email/password, so the
"password hashing/timing" surface reduces to recovery-code hashing (SHA-256 of a
40-bit random, appropriate) and TOTP comparison (`hmac.compare_digest`). Both are
sound. The findings are in the hand-rolled 2FA state machine.

---

## Finding 1 — 2FA can be silently disabled / taken over with no code (Medium)

**Where:** `data/twofa.py:216` `begin_enroll` (writes with `set()`, no `merge`,
never checks `enabled`); gated by `api_server.py:6070` `web_2fa_begin`, whose
only guard is `twofa.status()` — which **fails open** (`data/twofa.py:180-194`
returns `enabled=False` on a read error, unlike `is_enabled` at line 197 which
fails closed).

**Attacker steps** (attacker already holds a live web session for the victim —
the same threat model `web_2fa_disable`/`disable()` are built to resist: a
borrowed/stolen but still-signed-in browser):
1. The victim has TOTP enabled. `POST /api/v1/web/account/2fa/disable` is refused
   without a working code — the correct gate.
2. `POST /api/v1/web/account/2fa/begin` normally returns 409 ("already on").
   But its check reads `twofa.status()`, which returns `{"enabled": False}` on
   any Firestore read error (blip, transient outage, or cost-guard degradation).
3. In that window the call proceeds to `begin_enroll`, which does
   `_col().document(id).set({... "enabled": False, "recovery_hashes": [], ...})`
   with **no merge** — overwriting the live secret. `is_enabled` now returns
   False: **2FA is off, and no code was ever presented.**
4. Escalation: the attacker then `POST …/2fa/confirm` with a code from *their own*
   authenticator. `confirm_enroll` sees `enabled=False` and enables it — the
   attacker now owns the victim's second factor and receives fresh recovery codes.

**Reproduced:** `test_account_2fa.py` ("2FA silently disabled by /2fa/begin
during a Firestore read blip") and `test_account_surface.py` ("ESCALATION"):
begin turns an enabled factor off and a chosen secret is enabled in its place.

**Impact:** defeats the "disable needs a code" invariant during any read-failure
window; converts a session compromise into durable 2FA takeover + lockout.

**Fix:** make `begin_enroll` refuse to clobber an enabled record — read first and
return an error if `enabled` is set, or write with `merge=True` only into a
record it has confirmed is not enabled, ideally inside a transaction. And make the
`web_2fa_begin` gate fail **closed**: use `is_enabled` (fails closed) rather than
`status()` (fails open) to decide whether 2FA is already on, or surface the read
error as a 503 instead of proceeding.

---

## Finding 2 — Anti-replay in `verify()` is not atomic: one TOTP/recovery code accepted many times concurrently (Medium)

**Where:** `data/twofa.py:281` `verify` — read-modify-write on `last_counter`
(lines 298-310) and on `recovery_hashes` (lines 313-325) with no transaction /
compare-and-set. Two requests that arrive within one Firestore round-trip both
read the old `last_counter` (or the still-present recovery hash) and both pass.

**Attacker steps:** submit the *same* valid code many times in parallel (TOTP has
a 30s window and a ±1 step tolerance; a recovery code is valid until spent). All
concurrent submissions succeed.

**Reproduced:**
- `test_account_totp_replay.py`: one valid TOTP code accepted **10/10** times
  concurrently — the exact "a used code cannot be used again" property (module
  docstring, line 25) is void under concurrency.
- `test_account_2fa.py` ("recovery code single-use is not atomic"): one recovery
  code accepted **8/8** times.

**Impact:** the whole point of storing `last_counter` — that a shoulder-surfed
code is already spent when retyped — does not hold against parallel requests. A
code observed once (over-the-shoulder, a proxy, a leaked recovery sheet) can be
replayed as many times as the attacker fires it before the write lands. This is
the second-factor equivalent of a replayable OTP.

**Fix:** perform the counter bump and the recovery-code removal inside a Firestore
transaction (read `last_counter`/`recovery_hashes` and write the new value in one
`@firestore.transactional`, exactly as `claim_username` already does), so a code
whose counter is `<= last` — or a recovery hash already removed by a racing
request — is rejected. Fail closed on transaction contention.

---

## Finding 3 — Login-challenge attempt cap is not atomic (Low)

**Where:** `data/twofa.py:396` `resolve_login_challenge` — reads `attempts`,
compares to 5, then `ref.set({"attempts": attempts + 1})` (line 432). Concurrent
guesses all read the same `attempts` and are all judged; the counter lands at 1.

**Reproduced:** `test_account_2fa.py` ("sign-in challenge attempt cap is not
atomic"): **40/40** concurrent wrong guesses judged against one challenge though
stored `attempts` = 1.

**Impact:** the per-challenge "5 wrong then destroyed" cap is the tight bound on
guessing a 6-digit second factor within a challenge's 5-minute life; it is
defeated by parallelism. In practice the blast radius is limited by the endpoint
IP limiter (`web_auth_totp`/`auth_link_totp`: 20 / 300s per IP, confirmed in
`test_account_2fa.py`), so a single attacker is still IP-bounded; the exposure is
a distributed guesser getting far more than 5 tries per challenge. Defense-in-
depth regression rather than a standalone break — hence Low.

**Fix:** increment and test `attempts` in a transaction (same remedy as
Finding 2), so the 6th concurrent guess is refused regardless of timing.

---

## Brute-force math (checked, acceptable)

6-digit link code, `LINK_CODE_LIFETIME=180s`, one live code at a time
(`test_account_sessions.py`): a single IP gets 5 wrong guesses per 10 min before
`_LINK_FAIL_MAX` lockout → P(hit) ≈ 5e-6 per window. A distributed sweep at the
global backstop (600/min) fits ~1800 guesses in a code's life → **0.18%** per
code, and only yields a live token when `KSP_2FA_ENABLED=false`. Codes are
one-time and deleted on use. This is a sound design; the global cap is correctly
described in-code as a self-DoS backstop, not the brute-force defense.

TOTP challenge takeover of an account whose *first* factor is already held: 20
guesses/300s/IP × 3 valid codes / 1e6 ≈ 0.017 per IP-day → ~58 IP-days per
success. Acceptable, and Finding 3's fix keeps it that way against parallelism.

---

## Things checked and found sound

- **Account-id shape confusion:** `is_discord_account` = `str.isdigit()`;
  `firebase_account_id` unconditionally prefixes `a_` (even for an empty uid), so
  a web account id can never be a bare snowflake and the two namespaces are
  provably disjoint (`test_account_linking.py`).
- **Link Discord onto an existing account / merge:** `join_accounts` /
  `link_discord` refuse to merge two accounts that both have history
  (`LINK_HAS_DATA` / `JOIN_BOTH_ACTIVE`), never destroy a wallet, and cannot
  re-point a Discord id that already resolves to a live account. The Discord-side
  confirm flow additionally refuses to join an authority-holding Discord from a
  code (`cogs/account.py:_holds_authority`). Verified in `test_account_linking.py`.
- **Link-challenge replay:** `consume_link_challenge` is single-use; a second
  spend returns None.
- **Username:** claim is a Firestore transaction — a 4-way concurrent claim
  resolves to exactly one owner; reserved names are refused case-insensitively;
  there is no unauthenticated "is this name taken" endpoint, and the claim
  endpoint is authed + rate-limited so enumeration is bounded.
- **Session revocation / token epoch:** `logout_all_devices` bumps `token_version`
  and every previously-signed token (including the caller's) fails verification;
  tampered/wrong-key tokens are rejected; `_get_token_version` fails closed for an
  already-cached revocation across a read blip (`test_account_sessions.py`).
- **Audience split:** KSP vs web `aud` is carried and enforced; a copied KSP
  `session.token` does not open web endpoints (`get_web_user` → `_require_audience`,
  `allow_legacy=False`).
- **Auth coverage:** every `/web/account/*` mutation (username, display name,
  avatar, discord/code, all four 2FA routes, ksp/code, ksp/approve) sits behind
  `get_account_user`/`get_web_user` (`test_account_surface.py`).
- **Enrolment:** 2FA is never enabled on a secret nobody proved they can read
  (`begin` ≠ `confirm`); the enrolling code is spent and cannot immediately sign
  in (`test_twofa.py` already asserts this); the KSP-link vs web-signin challenges
  cannot complete each other's flow (payload presence check both ways).
- **Unverified-email sign-up** is refused (`web_auth_signin`), and a failed
  account resolution is 503, never a fresh empty wallet.
