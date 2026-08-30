# Admin Console & Privilege-Boundary Audit — `/api/v1/web/admin/*` + `/web/game/command`

Scope: owner/admin console privilege boundaries in `api_server.py`, gate helpers
(`get_owner`, `get_admin`, `get_web_user`, `_require_bot`), and
`data/craft_bans.py`, `data/mod_version.py`, `data/policy.py`,
`data/guild_config.py`, `cogs/perms.py`.

**Bottom line: no confirmed privilege-escalation or guild-scope-escape vulnerability was found.**
Every route is gated, the two tiers match the documented design exactly, guild
scope is re-checked on every guild-scoped *resource* (not just the tier), all
gates answer a uniform 404, and `/web/game/command` is allow-listed and
per-user scoped. Findings below are minor / defense-in-depth only, honestly
graded.

---

## Dependency table

Gate legend: **O** = `Depends(get_owner)` (bot-wide, single `BOT_OWNER_ID`),
**A** = `Depends(get_admin)` (owner OR mapped guild-admin role, scoped),
**W** = `Depends(get_web_user)` (any authed website session).
"Guild-scoped?" = does the handler re-check the *specific resource* against the
caller's admined guild set (`_admin_can_guild`)? "n/a-O" = owner-only, bot-wide by design.

| Endpoint | Gate | Guild-scoped on resource? | Verdict |
|---|---|---|---|
| `POST /web/game/command` | W | per-user (own uid/gid only) | correct |
| `GET /web/admin/whoami` | A | returns caller's own gids | correct |
| `GET /web/admin/overview` | A | guild list cut; **aggregate counts global** | correct-by-design (F2) |
| `GET /web/admin/listings` | A | y — filters `_admin_can_guild(l.guild_id)` for non-owner | correct |
| `PATCH /web/admin/listings/{id}` | A | y — 404 if listing's guild out of scope | correct |
| `DELETE /web/admin/listings/{id}` | A | y — 404 if out of scope | correct |
| `GET /web/admin/craftbans` | O | n/a-O | correct |
| `GET /web/admin/craftbans/preview` | O | n/a-O | correct |
| `POST /web/admin/craftbans` | O | n/a-O | correct |
| `DELETE /web/admin/craftbans/{hash}` | O | n/a-O | correct |
| `POST /web/admin/craftbans/backfill` | O | n/a-O | correct |
| `GET /web/admin/users` | O | n/a-O | correct |
| `POST /web/admin/users/{id}/adjust` | O | n/a-O | correct |
| `POST /web/admin/users/{id}/logout_all` | O | n/a-O | correct |
| `POST /web/admin/users/{id}/suspend` | O | n/a-O | correct (F3) |
| `DELETE /web/admin/users/{id}/suspend` | O | n/a-O | correct |
| `DELETE /web/admin/users/{id}` | O | n/a-O | correct |
| `POST /web/admin/message` (DM) | O | n/a-O | correct |
| `POST /web/admin/announce` | A | y — guild from body checked; role/channel resolved from that guild | correct |
| `GET /web/admin/guilds` | A | y — iterates, skips `not _admin_can_guild` | correct |
| `POST /web/admin/channels/{id}/lock` | A | y — guild from body checked; channel resolved from that guild | correct |
| `GET /web/admin/modversion` | O | n/a-O | correct |
| `POST /web/admin/modversion/publish` | O | n/a-O | correct (F4) |
| `GET /web/admin/costs` | O | n/a-O | correct |
| `POST /web/admin/costs/refresh` | O | n/a-O | correct |
| `GET /web/admin/controls` | O | n/a-O | correct |
| `POST /web/admin/controls` | O | n/a-O | correct |
| `POST /web/admin/policy/bump` | O | n/a-O | correct |

The tier split is exactly the design: guild-scoped moderation = `get_admin`;
every bot-wide lever (craft bans, user accounts/suspend/delete, DM, DLL publish,
costs, runtime controls, policy) = `get_owner`. **No owner-only power is
reachable via the admin tier.**

---

## Why the boundary holds (verified checks)

- **Uniform 404, no existence leak.** `get_owner` (`api_server.py:7329`) and
  `get_admin` (`api_server.py:7364`) both raise `404 "Not found"` for non-eligible
  callers — never 403. Out-of-scope *resources* (a listing in another guild) also
  404 identically to a missing one (`admin_edit_listing`/`admin_delete_listing`,
  `api_server.py:7570`, `7614`), so scope cannot be probed by response.
- **Guild scope resolved live, per request** (`_admin_role_guild_ids`,
  `api_server.py:7340`): iterates `_bot_instance.guilds`, resolves the mapped
  `admin` role per guild, checks live member-cache membership. Revoking the
  Discord role revokes console access on the next request. Offline bot → `[]` → 404 (fail-closed).
- **No admin-role default.** `guild_config._role_default("admin")` returns `None`
  (`data/guild_config.py:119`) — no settings.py fallback; guild administrators are
  NOT auto-admins, matching `cogs/perms.is_admin_user`.
- **Owner id cannot be spoofed** (`_is_owner_id`, `api_server.py:7308`): a
  non-owner snowflake returns `False` with no lookup; a non-digit account id is
  resolved through its linked Discord. `OWNER_ID=0` matches nobody.
- **Web audience enforced.** Console chains through `get_web_user` →
  `_require_audience(AUD_WEB, allow_legacy=False)` (`api_server.py:749`): a copied
  KSP `session.token` cannot reach the console, and a suspended account is refused first.
- **`user_id` adjust cannot mint a ghost wallet** (`admin_user_adjust`,
  `api_server.py:7915`): rejects any id not already in the store (404) before
  calling `get_user`.
- **Channel-lock / announce cannot cross guilds** (`api_server.py:8247`, `8162`):
  scope checked on `req.guild_id`, then `channel_id`/`role_id` resolved via
  `guild.get_channel`/`guild.get_role`, which only return objects inside that guild.
- **`/web/game/command` is tight** (`api_server.py:7118`): command must be in
  `_GAME_COMMANDS` (`{"open_submit"}`), `contract_id` must match
  `^[A-Za-z0-9_-]{1,64}$`, contract must belong to the caller, and `push_frame`
  targets only `(gid, str(uid))`. The mod re-validates against its own token.
- **Craft bans owner-only** (all five routes `Depends(get_owner)`).
- **Cheat gate is not console-flippable.** `admin_set_controls`
  (`api_server.py:8391`) exposes only `version_check`, `device_binding`,
  `cost_guard_enabled`, and two budgets — all owner-only. No route flips
  `KSP_CHEAT_DISQUALIFY_ENABLED`.

---

## Findings (minor / defense-in-depth — most severe first)

### F1 — Owner can publish a mod version with an arbitrary `download_url`
- Severity: **Low** (owner-only; defense-in-depth). PLAUSIBLE.
- `api_server.py:8292` `admin_publish_version` — `download_url` accepted verbatim
  (`Form("")`), stored via `mver.publish_version`, and surfaced to clients as the
  update gate's "Download latest" link. No scheme/host validation.
- Attack: only `BOT_OWNER_ID` can reach it (not a guild admin), so not an
  escalation — but a compromised owner session or a mistake could point every
  client's update prompt at an attacker-controlled URL.
- Fix: require `https://` and, ideally, an allow-listed host before storing.

### F2 — Guild admins see global aggregate counts in `overview`
- Severity: **Info** (documented as intentional). CONFIRMED.
- `api_server.py:7495` `admin_overview` — guild *list* is cut to the caller's
  guilds, but `users`, `listings_active/delisted`, `suspensions_active`,
  `policy_version`, `mod_version` are bot-wide totals shown to any guild admin.
  In-code comment states counts are "read-only facts, not levers".
- Impact: aggregate magnitudes only — no record contents, ids, or cross-guild
  resource access. Accept, or gate counts behind `is_owner`.

### F3 — `suspend` bounds check does not reject NaN hours
- Severity: **Low** (owner-only robustness). CONFIRMED.
- `api_server.py:7988` `admin_user_suspend` — `req.hours < MIN or req.hours > MAX`
  lets `float('nan')` through (both comparisons False). `suspensions.suspend`
  (`data/suspensions.py:134`) `max(MIN, min(nan, MAX))` still yields `nan`,
  producing `until = now + nan`.
- Impact: owner-only; corrupts a record, crosses no boundary. Fix: reject
  non-finite `hours` (`math.isfinite`) in the endpoint and clamp in `suspend`.

### F4 — (verified NOT a gap) `modversion/publish` payload
- DLL size capped (20 MiB, `api_server.py:8311`), empty upload rejected, hash
  computed from bytes, bare `sha256` accepted only when no DLL given. Route is
  `get_owner`; a guild admin cannot reach it. Only residual is `download_url` (F1).

---

## Attacker scenarios tested — all FAIL (boundary holds)

- Guild-admin of A edits/deletes a listing from B → `_admin_can_guild` False → **404**.
- Guild-admin announces/locks into B → guild from body, scope False → **404**.
- Guild-admin issues craft ban / adjusts wallet / suspends / DMs / publishes DLL /
  flips cost guard / bumps policy → route `get_owner`, `_is_owner_id` False → **404**.
- Non-admin authed user probes any admin route → 404 everywhere; no 403, no
  distinct body. Existence hidden.
- KSP `session.token` in a browser → `get_web_user` rejects non-web audience → **401**.
- `/web/game/command` with another user's `contract_id` → ownership 404, and
  `push_frame` keyed to caller's own `(gid, uid)`.
- `users/{id}/adjust` with a made-up id → 404 before any write (no wallet minted).
