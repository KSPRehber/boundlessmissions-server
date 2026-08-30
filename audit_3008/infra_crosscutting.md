# Cross-cutting infrastructure audit — 2026-08-30

Scope: app setup, CORS, request-size/DoS middleware, dangerous sinks (RCE/deser),
SSRF, HMAC secret handling, /health, rate-limiter memory. (The 4 endpoint agents
own auth, economy, admin, and craft/input surfaces respectively.)

## Result: no cross-cutting vulnerabilities found. Design is sound. One minor hardening gap.

### CONFIRMED-SAFE (verified, no action needed)
- **CORS** (api_server.py:116-123): middleware added only when `API_CORS_ORIGINS`
  is explicitly set; no wildcard, `allow_credentials` not enabled. Default = no
  cross-origin headers. Correct.
- **Docs/OpenAPI** (api_server.py:109-111): `/api/docs` + `/api/openapi.json`
  gated behind `API_DOCS_ENABLED` (off in prod). Surface not enumerable.
- **Request-size / gzip-bomb DoS** (api_server.py:178-219): dual guard — a
  Content-Length middleware (413 up front) AND `_BodyCapMiddleware` that counts
  streamed bytes to defeat `Transfer-Encoding: chunked` bypass. Per-file caps
  (`_read_upload`) and decompressed caps (`_safe_gunzip`, 64 MB) documented.
  Blueprint cap auto-derives from render scale. Thorough.
- **No RCE/deserialization sinks**: grep for eval/exec/os.system/subprocess/
  pickle/marshal/yaml.load/__import__ across api_server.py, api_auth.py,
  contract_actions.py, data/*.py → none.
- **SSRF via `cdb.download_url()`** (data/contracts.py:304): the 3 callers
  (contract_views.py:974, api_server.py:4518, 8790) all pass a stored `url` that
  is a SERVER-GENERATED storage path from `upload_private_to_storage`
  (api_server.py:4232→4236, 4243→4247). `is_storage_path()` signs bucket paths;
  the raw-fetch branch only runs on http(s) values, which a client cannot write
  into those docs (file entries are built server-side from multipart uploads, not
  client JSON). No client-steerable fetch → no SSRF to 169.254.169.254 / localhost.
- **HMAC secret** (config.py:158-164): server hard-fails at startup if
  `API_SECRET_KEY` is blank or a known placeholder while KSP API is enabled;
  `API_SECRET_KEY_PREVIOUS` blanked if placeholder or == current. Token forgery
  via known key is prevented. Signing = HMAC-SHA256, `compare_digest`, expiry +
  token-version revocation, aud separation (api_auth.py:675-708).
- **/health** (api_server.py:8734): returns static `{status, version}` only. No leak.
- **Rate-limiter memory** (api_server.py:554-567): `_sweep_rate_buckets` drops
  buckets with no hit in 24h, every 5 min. Keys bounded by real peer IP
  (X-Forwarded-For honored only from configured trusted proxies, `_client_ip`).

### MINOR (LOW/INFO) — hardening gap
- **No entropy floor on `API_SECRET_KEY`** (config.py:158): validation rejects
  the placeholder set but accepts ANY other non-placeholder value, including a
  short/weak one (e.g. `hunter2`). Since the token payload is base64-visible, an
  attacker holding any one token can brute-force a weak HMAC key offline, then
  forge tokens for any user. Fix: require e.g. `len >= 32` (or a shannon-entropy
  check) in the same startup guard. Low likelihood (operator error) but the
  blast radius is total (universal forgery), so cheap to add.
