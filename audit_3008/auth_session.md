# Auth & Session Surface — Security Audit (audit_3008)

Scope: `api_auth.py`, `data/twofa.py`, `data/accounts.py`, `data/suspensions.py`,
and the auth/session endpoints + dependency helpers in `api_server.py`
(`get_current_user`, `get_web_user`, `get_user_token_only`,
`get_user_allow_suspended`, `_require_audience`, `enforce_not_suspended`,
`check_device`, `create_device_challenge`, `_dm_device_approval`, the link/2FA/
device/attest/ws-ticket/version endpoints and the `/web/*` account endpoints).

Method: each handler read end-to-end, inputs traced request -> storage. Token
model, secret handling, and rate-limit math verified against `config.py` /
`settings.py`.

**Bottom line:** this is a well-hardened surface. No confirmed auth bypass, no
token forgery path, no cross-audience escalation, and no IDOR were found — the
token spine (HMAC + `compare_digest`, `tv` revocation, `aud` split) is sound and
every fail-open is documented and defensible. The findings below are hardening
notes and acknowledged residual risks; the most material is the legacy
trust-on-first-use device-adoption window (MEDIUM, requires a pre-stolen token).

---

## 1. Trust-on-first-use device adoption can bind an attacker's device (legacy / no-device accounts)

- **Severity:** MEDIUM
- **Status:** CONFIRMED (code path); acknowledged rollout tradeoff
- **Location:** `api_auth.py:445-466` (`check_device`), reached from
  `api_server.py:807` (`get_current_user`)

**Path.** `check_device` returns `"ok"` and *permanently adopts* the first device
id it sees whenever the account's `allowed_devices` set is empty:

```
if not allowed:
    if device_id:
        add_allowed_device(user_id, device_id)   # PERMANENT trust
    return "ok"
```

**Attack.** An attacker who already holds a **copied KSP session token** for an
account whose `allowed_devices` is empty can send one gated request with their own
`X-Device-Id` and become the account's trusted device — with no DM approval. Their
device is then trusted forever, and the legitimate owner's device becomes the one
that gets the `device_unverified` challenge.

**Preconditions (why not HIGH).** The attacker must already possess a valid token,
*and* the account must have an empty trust set. That set is empty only for: (a)
sessions minted before device binding shipped (legacy 30-day tokens), or (b) a KSP
link performed with no `X-Device-Id` header (all shipped clients send one, so this
is edge). Every freshly linked client binds its own device at link time
(`_issue_link_token`, `api_server.py:884`), so the normal case is protected — a
second device is correctly challenged.

**Fix.** For the legacy window, prefer challenging the first-seen device (DM
approval) over silently adopting it, or bound trust-on-first-use to a short grace
period after the rollout timestamp. At minimum, log a warning when TOFU adopts a
device so an owner can notice a wrong adoption. The same fail-open on a Firestore
outage (`check_device` returns `"ok"` when the trust set is *unknowable*) is a
narrower, time-boxed version of the same exposure (see Finding 6).

---

## 2. No minimum length / entropy floor on the token-signing secret

- **Severity:** LOW
- **Status:** CONFIRMED
- **Location:** `config.py:151-166`

The startup guard rejects a blank secret and a small blocklist of known
placeholders (`_DEFAULT_API_SECRETS`), but accepts **any** other value — including
`API_SECRET_KEY=x`. That secret is the sole thing standing between an attacker and
forging a token for any `uid` (`_sign_token`/`_verify_token`, `api_auth.py:675-708`).
A short/low-entropy operator-chosen key is offline-brute-forceable against a single
captured token (the payload is public base64; only the HMAC needs matching).

**Fix.** Add a length/entropy floor (e.g. refuse `< 32` chars) to the same guard
that already rejects placeholders. The error message already recommends
`token_urlsafe(48)`; enforce it.

---

## 3. Cross-surface 2FA-challenge redemption mints a WEB token from a KSP-link challenge

- **Severity:** LOW (no privilege escalation)
- **Status:** CONFIRMED
- **Location:** `api_server.py:5867-5898` (`web_auth_totp`) vs
  `api_server.py:998-1021` (`auth_link_totp`); challenges share
  `data/twofa.py` `twofa_login` collection.

Both web sign-in and KSP linking store their 2FA login challenges in the **same**
`twofa_login` collection. `auth_link_totp` correctly refuses a challenge that
carries no link `payload` (a web sign-in challenge) — so a **web->KSP** upgrade is
blocked, which is the dangerous direction (a KSP token is device-bound and can
strip trusted devices). Good.

`web_auth_totp`, however, **ignores** the payload, so a **KSP-link** 2FA challenge
can be completed at `/web/auth/totp` to mint an `AUD_WEB` token for that account.
This is **not** an escalation: the caller must present the account's TOTP code, and
the token is for the same account on a *lower*-privilege tier. But it is an
unintended cross-flow.

**Fix.** Tag each challenge with its purpose (`kind: "signin" | "link"`) and have
`web_auth_totp` refuse a `link` challenge, mirroring the guard `auth_link_totp`
already has. Defensive symmetry, not a live hole.

**Note (defense observed):** the more dangerous direction is already closed by the
`if not payload` check at `api_server.py:1013-1018`.

---

## 4. A WebSocket opened before suspension keeps streaming; suspension doesn't close live sockets

- **Severity:** LOW
- **Status:** CONFIRMED
- **Location:** `api_server.py:4693-4742` (`issue_ws_ticket` / `notifications_ws`),
  `suspensions.suspend` (`data/suspensions.py:128`)

Ticket issuance is correctly behind `get_user_token_only` (suspension enforced), so
a suspended user cannot *obtain* a new WS ticket. But `suspend()` does not close
existing sockets (only `logout_all` does, via `_hub.close_user`, at
`api_server.py:1429`). A client connected before the suspension keeps receiving that
account's notifications until it disconnects. Impact is limited — the WS delivers
notifications only, no money/state actions — but it is an inconsistency with the
stated suspension model ("every token-gated endpoint is covered").

**Fix.** Call `_hub.close_user(user_id)` from the suspend path (or a small
suspension->hub hook), the same way `logout_all` does.

---

## 5. Per-IP rate-limit/lockout collapses to global if `API_TRUSTED_PROXIES` is misconfigured behind a proxy

- **Severity:** LOW (self-DoS, not a bypass)
- **Status:** CONFIRMED (deployment-dependent)
- **Location:** `api_server.py:593-622` (`_client_ip`, `_guard_link_attempt`)

`_client_ip` **safely** ignores `X-Forwarded-For` unless the direct peer is in
`API_TRUSTED_PROXIES` — so an attacker cannot spoof XFF to mint fresh buckets
(good, this is the important property). The failure mode is the opposite: if the
server runs behind a reverse proxy and `API_TRUSTED_PROXIES` is left **unset**,
every request's `peer` is the proxy IP, so the per-IP link limit and the
5-wrong-codes lockout (`_LINK_FAIL_MAX`) become **global** — 5 wrong link guesses
from anyone lock out *all* linking for 10 minutes, and per-IP brute-force
protection degrades to the coarse global cap.

**Fix.** No code change needed; document/verify `API_TRUSTED_PROXIES` is set in
production. Optionally warn at startup if the bind looks proxied but the list is
empty.

---

## 6. Fail-open reads on the security-critical caches (documented, intentional)

- **Severity:** INFO / accepted risk
- **Status:** CONFIRMED, by design
- **Location:** `api_auth.py:106-126` (`_get_token_version`),
  `api_auth.py:354-377` (`_get_allowed_devices`) + `check_device`,
  `data/suspensions.py:89-114` (`get_active`)

Token-version, allowed-devices, and suspension reads all **fail open** on a
Firestore error (accept the token / skip the device gate / treat as not suspended).
This is deliberate and consistently reasoned (an outage that logged out or locked
out every player is judged worse than a stolen token getting extra minutes). The
residual risk during a Firestore outage: a just-revoked token keeps working, a
copied token on a new device passes the gate, and a suspended user is briefly
un-suspended. Each is time-boxed to the outage and the caches never *persist* a
failed-read guess. Correctly implemented; noting the residual exposure only.

Counter-note: `twofa.is_enabled` (`data/twofa.py:197-213`) deliberately fails
**closed** (returns `True`), which is the correct inversion for a second-factor
gate — verified.

---

## 7. `KSP_2FA_ENABLED=false` turns a brute-forced link code directly into a token

- **Severity:** INFO
- **Status:** CONFIRMED (default is `true`, so not a live risk)
- **Location:** `api_server.py:1056-1057`, brute-force math in
  `api_server.py:625-666`, `settings.py:523-524`

With `KSP_2FA_ENABLED=false` (dev/testing), a correct link code yields a session
token with no second step. Brute-force is impractical regardless: 6-digit space =
1e6, codes live 180s, per-IP lockout after 5 wrong guesses / 10 min, and only a
handful of codes are ever live — at the global cap the hit probability over a
code's life is ~0.2% (matches the in-code analysis). With the default
`KSP_2FA_ENABLED=true`, even a lucky hit only yields an `approval_required`
challenge that still needs the DM/panel confirmation the attacker cannot satisfy.
No action needed beyond keeping the default on in production.

---

## Defenses verified as correct (no finding)

- **Token verification:** HMAC-SHA256 over canonical JSON, compared with
  `hmac.compare_digest`; expiry enforced; not JWT, so no `alg:none`/alg-confusion.
  Payload is unforgeable without the secret. (`api_auth.py:685-708`)
- **`aud` split:** legacy (`aud=None`) accepted on the KSP tier only
  (`allow_legacy=True`); web tier refuses legacy *and* KSP tokens — the exact
  copied-`session.token`-opens-web-money attack is closed. No sensitive endpoint
  uses `get_user_token_only` directly (only device-poll/report/attest/ws-ticket/
  debug/suspension/logout — all either-audience-safe). (`api_server.py:728-758`)
- **`tv` revocation:** `logout_all` bumps version + updates in-proc cache + closes
  live sockets; `join_accounts`/`purge_ksp_user_data` bump-not-delete to avoid
  resurrecting tokens. (`api_auth.py:791-815`, `data/accounts.py:886-897`)
- **TOTP:** RFC-6238 with `compare_digest`, +/-1 step window, `last_counter` replay
  block, recovery codes hashed + single-use, 5-attempt cap per login challenge,
  144-bit challenge ids. (`data/twofa.py`)
- **Device / login approval:** owner-checked server-side
  (`resolve_approval`/`resolve_device_challenge` compare `acting_user_id`), Discord
  button identity is non-spoofable, challenge ids unguessable; approval must land on
  the surface that did *not* consume the code. (`api_auth.py:271-295, 504-533`)
- **IDOR checks:** `device_report` verifies `target.user_id == token.user_id` and
  404s otherwise; `attest/respond` binds the challenge to the token's user;
  `web_account_ksp_approve` re-checks challenge ownership; account endpoints key off
  the token's `uid`, never a client-supplied id. (`api_server.py:1237, 1625, 6319`)
- **`debug/signtest`:** 404 unless `DEBUG_ENDPOINTS_ENABLED` (default false); signs
  GCS URLs, not session tokens — not a token oracle. (`api_server.py:1655-1673`)
- **ws-ticket / attestation:** single-use, TTL'd, high-entropy; ticket path removed
  the token-in-URL leak. (`api_server.py:4693-4728`)
- **Firebase sign-in:** `verify_id_token(check_revoked=True)`, unverified-email
  refused, `account_for_firebase`=None (unreadable) refused rather than treated as
  new. (`api_server.py:5795-5864`)
- **Account link takeover:** `/b account` uses peek-then-confirm with the target
  account named before binding, and `join_accounts` keeps the history-bearing side.
  (`cogs/account.py:118-209`, `data/accounts.py:789-901`)
