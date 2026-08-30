# Audit 3008b — authorization: admin/owner console, moderator tooling, suspensions, mimic

Scope: `/api/v1/web/admin/*` + `get_owner`/`get_admin`/`get_user_token_only`/`get_user_allow_suspended`
(`api_server.py`), `cogs/perms.py`, `data/suspensions.py`, `cogs/targets.py`, the `bot.py` mimic patch,
and every slash command gated with `default_permissions`.
PoCs: `audit_3008b/test_admin_{console,targets,suspension,slash}.py` (`./run_admin.sh`). Every finding
below is a `BUG` line from one of them; nothing here touched Firestore or Discord.

## Findings

### 1. HIGH — a username can be spelled as another player's Discord id, and the username wins
`data/accounts.py:402-404` (`USERNAME_MAX = 20`, `_USERNAME_RE` allows digits-only), `cogs/targets.py:196-205`.
PoC: `test_admin_targets.py`.

`targets.resolve` documents that a moderator may paste an **account id** into the `username` field, and
tries `owner_of_username` first "because a name can never look like a snowflake". It can: `validate_username`
accepts any 3–20 character `[A-Za-z0-9_-]` string, and a snowflake is 17–19 digits.

Attack: claim the username `190212345678901234` (the victim's Discord id) on the website. Any moderator who
runs `/givemoney username:190212345678901234`, `/setbalance …`, `/setxp …`, `/fine …` or `/contractreset …`
(all of which route through `resolve`) acts on the **attacker's** account; the confirmation even reads
"Totally Jeb (@190212345678901234)". Wrong-direction cases (a fine aimed at the attacker's victim) land on
the attacker, so the attacker chooses which way it pays.

Fix (both): in `validate_username`, reject names that are all digits or start with `accounts.FIREBASE_PREFIX`
(`a_`) — they are id shapes, not names; and in `resolve`, when the field is id-shaped (`isdigit()` /
`a_`-prefixed) resolve the id branch **first** and only fall back to the username lookup if no account
exists for it. Sweep existing `usernames/` reservations for digit-only ids.

### 2. MEDIUM — four moderator commands have no in-code authority check at all
`cogs/contracts.py:76-79` (`/contractreset`), `cogs/corps.py:596` (`/corpsgenerate`), `cogs/corps.py:658`
(`/corpsprivacy`), `cogs/tickets.py:634` (`/ticketpanel`). PoC: `test_admin_slash.py` (`Command.checks == []`).

`@app_commands.default_permissions(...)` is only a *default*: any server administrator can grant the command
to any role or @everyone in Server Settings → Integrations, and the bot then runs it with no check of its
own. `/contractreset` is the one that matters — it cancels every PENDING/ACTIVE/SUBMITTED/DISPUTED contract
of any account (by `username:` too, so web-only players are reachable), refunds escrow to issuers and
restores rescue wrecks. Every other mod command (`/setbalance`, `/fine`, `/givemoney`, `/removeroles`,
`/gkchannel`, `/add_custom_mission`) carries a real `mod_only()`/`is_mod_user` check; these four were missed.

Fix: add `@mod_only()` / a `perms.is_mod_user` predicate to `/contractreset`, and `perms.is_admin_user`
(mapped bot-admin role) to `/corpsgenerate`, `/corpsprivacy`, `/ticketpanel`, matching CLAUDE.md's
"guild administrators are not auto-admins". Optionally `@app_commands.guild_only()` on all four (none is
DM-safe).

### 3. MEDIUM — `/setxp` and the console's `xp_set` can stall the whole bot
`data/store.py:314-319` (`level_from_xp`), `cogs/xp.py:193-215`, `api_server.py:7946` (`store.set_xp`).
PoC: `test_admin_slash.py` (`level_from_xp(2**53)` does not return in 3 s).

`level_from_xp` counts levels one at a time (`LEVEL_XP_EXPONENT = 1.5`, so ~`(xp/100)^(2/3)` iterations).
A Discord integer option carries up to 2^53: `/setxp amount:9007199254740991` — available to any guild
**administrator**, not only bot admins — runs ~2e9 iterations inside `store.set_xp`, on the event loop,
holding `store._lock`. Discord heartbeats stop, every command and API request that needs the store hangs.
The owner console's `xp_set` is unbounded `Optional[int]` (2^70 is worse) — see also #4.

Fix: clamp XP at a sane ceiling (`settings`-level `XP_MAX`) in `set_xp` and both callers, and make
`level_from_xp` closed-form or capped (`min(level, LEVEL_MAX)`), so no input can iterate more than a few
hundred times.

### 4. MEDIUM (owner-only) — `admin_user_adjust` accepts values Firestore cannot store; one bad write poisons every flush
`api_server.py:7463-7472` (`AdminUserAdjust`), `7927-7956`; `data/store.py:445-485` (`save`).
PoC: `test_admin_console.py`.

`balance_set`, `balance_delta`, `xp_set` are bare `Optional[int]`. `balance_set=2**70` returns 200 and lands
in memory; Firestore's encoder raises `ValueError: Value out of range` on it. `store.save()` puts **all**
dirty users in one batch, catches the exception and re-adds them all to `_dirty_users` — so from that
moment no user record is ever persisted again (every 5-minute flush fails on the same document) until a
restart, which then drops everything unsaved. Owner-only, so a footgun rather than an attack, but a typo'd
extra digit in the console is enough, and nothing reports it beyond a log line.

Fix: `Field(ge=-2**53, le=2**53)` (or a game-level cap) on all three; in `store.save`, fall back to
per-document writes when the batch fails so one bad record cannot hold the rest hostage.

### 5. LOW — a suspension lift during a Firestore blip un-suspends the player for a TTL and lies to the console
`data/suspensions.py:171-181` (`lift`). PoC: `test_admin_suspension.py`.

`lift` calls `get_record`, which returns `None` on a read failure. `_active(None)` is `None`, so `lift`
returns `False` ("nothing was running") **and** writes `_cache[user_id] = (None, now)`. `get_active` — the
gate every token-hits — then answers "not suspended" for 30 s from that entry although the document is still
active, and the console tells the moderator no suspension existed. The gate's own read path deliberately does
*not* cache a failed read (`get_active`, line 128); `lift` should follow it.

Fix: have `get_record` distinguish "absent" from "unreadable" (return a sentinel / raise), and make `lift`
refuse with an error — and cache nothing — when it could not read.

### 6. LOW — `/linkcode` (and `/linkas`) mint the code for the raw snowflake, not the account it resolves to
`cogs/ksp_bridge.py:352-365`, `cogs/admin.py:491-493`, `api_server.py:861-890` (`_issue_link_token`).
PoC: `test_admin_slash.py` (source assertion; the `join_accounts` branch that keeps the web side is in
`data/accounts.py:824-827`).

`join_accounts` keeps the **web** account (`a_…`) when it has history and the Discord side has none, and
points `account_discord/{snowflake}` at it. `/linkcode` then still calls `generate_link_code(gid,
interaction.user.id, …)`, and `_issue_link_token` mints the KSP/web token with that snowflake — which
`ensure_discord_account` resolves to the kept account but the token does not. Consequences: the game plays
on an empty orphan wallet, and a console suspension issued on the account id the Users tab shows (`a_…`)
does **not** cover that token (`enforce_not_suspended` keys on the token's `user_id`). `admin_user_delete`
of the `a_…` account likewise leaves the snowflake session alive.

Fix: resolve `accounts.account_for_discord(interaction.user.id)` in `/linkcode` and `/linkas` (refusing on
`None`, as `targets.resolve` does) before minting; or resolve in `_issue_link_token`, the single funnel.

### 7. LOW — the guild tier is served owner-only state on `/overview`
`api_server.py:7514-7546`. PoC: `test_admin_console.py`.

A mapped guild admin receives `mod_version.latest_hash` (the attestation hash), `version_check_enabled`,
`device_binding_enabled`, `policy_version`, the global `suspensions_active` count and the global user count.
None of these are guild-scoped and all correspond to owner-only tabs. Knowing device binding is off is the
one with some teeth (it tells a role holder when a copied token works from any machine).

Fix: build the dict, then `if not user["is_owner"]: drop mod_version/gate flags/suspensions_active/users`.

## Informational (not filed as findings)

- A plain (non-admin) session posting **malformed JSON** to an admin route gets 422 while a bogus path gets
  404 — route existence is revealed, but the routes are public source, and Pydantic-invalid bodies / bad
  query params correctly 404 (dependency solved before validation; verified for JSON, query and multipart).
- A request with **no** Authorization header gets 401 from `get_web_user` before the 404 tier gate; same
  reasoning.
- Mimic: the `exclude=("mimic","unmimic")` check works because `Interaction.command` is a lazily resolved
  cached property, so `/unmimic` runs as the real owner. Verified.

## Verified sound

- `get_owner` / `get_admin`: 404 to anyone else for every route (GET/POST/DELETE, including
  `users/*`, `controls`, `policy/bump`, `craftbans`, `message`); a guild admin cannot reach any owner route.
- `_is_owner_id`: `OWNER_ID=0` matches nobody; a non-snowflake id resolves through `discord_for_account`,
  and the only writer of an account's `discord_id` is the Discord-side "Link it" button
  (`cogs/account.py`), which additionally refuses when the Discord holder has authority anywhere — so a
  web account cannot claim the owner's snowflake to inherit the console.
- Guild scoping: listings list/edit/delete filter on the listing's `guild_id`; `announce` and
  `channels/{id}/lock` check `_admin_can_guild` on the body's `guild_id` and look the channel/role up
  *through that guild* (`Guild.get_channel/get_role` are per-guild), so an out-of-scope id in either the
  path or the body is a 404; the guild picker hides other guilds; non-numeric ids 404 rather than 500.
- Admin-role membership is resolved live from the member cache on every request; an offline bot yields no
  authority.
- `admin_user_adjust` refuses ids not in the store (no ghost wallet); negative `balance_set` clamps to 0;
  the owner cannot suspend or delete their own account; `suspend` rejects NaN/inf/out-of-range hours with
  422 and `suspensions.suspend` clamps again (`MAX_HOURS`, `MIN_HOURS`).
- Suspension cache: a suspend/lift is visible on the next request; the TTL is clamped to the time left so
  an expired suspension is never enforced from cache; a failed read on the gate fails open without caching.
- Every token-gated endpoint passes through `get_user_token_only` → `enforce_not_suspended`; the only
  `get_user_allow_suspended` users are `/auth/suspension` and `/auth/logout_all`, as documented; the WS
  ticket is issued through the suspended gate and `_hub.close_user` drops live sockets on suspend.
- Runtime gate flips (`controls`), DLL publishing (https-only download URL), policy bump, craft bans,
  DM-from-bot and user accounts are all owner-tier.
- `targets.resolve`: both fields → refused; neither on a write → refused; a failed account/index read is
  refused as "try again" (never falls back to `member.id`); an unknown id mints no wallet.
- `cogs/perms.py`: every check gates on `real_user`; `/mimic`, `/unmimic`, `/linkas`, `/publishversion`,
  `/policyversion`, `/reload`, `/shutdown` are owner-only; `/setbalance`, `/fine`, `/givemoney`,
  `/removeroles`, `/gkchannel` carry `mod_only()`; `/add_custom_mission` checks the real invoker in-body.
