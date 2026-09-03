# Boundless Missions: Discord Bot & API Server

> A **discord.py 2.x** bot for the Unified Players of KSP (UPoK) community, with
> a **FastAPI** server running in the same process. The bot is the community
> side: economy, XP, corporations, moderation, auctions. The API is the game
> side: the KSP mod and the website both talk to it, and neither ever sees a
> secret.

One process, two faces. Discord and the API are concurrent asyncio tasks started
by `bot.py`; they share one Firestore-backed store, one config, one cost meter.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Slash Commands](#slash-commands)
5. [The KSP API](#the-ksp-api)
6. [The Website API](#the-website-api)
7. [Owner & Admin Console](#owner--admin-console)
8. [Data & Persistence](#data--persistence)
9. [Contracts](#contracts)
10. [The Economy: Debt, Garnishment and the Ledger](#the-economy-debt-garnishment-and-the-ledger)
11. [Friends, Quicksend and the Crew Ledger](#friends-quicksend-and-the-crew-ledger)
12. [Moderation: Craft Bans, Reports and Suspensions](#moderation-craft-bans-reports-and-suspensions)
13. [The Mod Version Gate](#the-mod-version-gate)
14. [AI Integration](#ai-integration)
15. [Cost Guard](#cost-guard)
16. [Security Notes](#security-notes)
17. [What Discord No Longer Does](#what-discord-no-longer-does)
18. [Tests](#tests)
19. [Related Docs](#related-docs)

---

## Quick Start

```bash
cd "GK Discord Bot"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env            # fill in the required values below

python bot.py           # normal start
python bot.py --sync    # only after adding or changing slash commands
```

**Only pass `--sync` when slash commands have actually changed.** Syncing on
every restart will hit Discord's rate limit.

### Required configuration

| Variable | What it is |
|----------|------------|
| `DISCORD_TOKEN` | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `BOT_OWNER_ID` | The single Discord user id that owns the bot (see [Owner & Admin Console](#owner--admin-console)) |
| `FIREBASE_CREDENTIALS` | Path to the Firebase Admin SDK service-account JSON |
| `FIREBASE_STORAGE_BUCKET` | Firebase Storage bucket for crafts, renders and flags |
| `API_SECRET_KEY` | HMAC key that signs KSP session tokens |
| `GEMINI_API_KEY` | Optional. Without it every AI call degrades to a fallback rather than failing |

`.env.example` is the full list, grouped and commented, including the API
server, TLS, cost-guard budgets and the runtime gates. Gameplay balance
(XP curves, cooldowns, economy rates, auction and marketplace limits, channel
and role ids) lives in **`settings.py`**, which is safe to commit and has no
secrets in it.

### Command grouping

`COMMAND_GROUP` nests every command under one group: `COMMAND_GROUP=g` gives
`/g help`, `/g balance`, … It defaults to **empty**, which leaves commands at the
top level. Admin commands (`default_permissions=administrator`) always go under a
top-level `admin` group and moderation commands under `mod`, regardless. The
nesting is applied at startup in `bot.py:setup_hook`.

---

## Architecture

```
                       ┌──────────────────────────────────┐
   Discord  ◄────────► │  GeneKermanBot  (discord.py 2.x) │
                       │    cogs/  ·  i18n  ·  perms      │
                       ├──────────────────────────────────┤
   KSP mod  ◄────────► │  api_server.py  (FastAPI)        │
   Website  ◄────────► │    /api/v1/…  ·  /ws/v1/…        │
                       ├──────────────────────────────────┤
                       │  data/store.py  (buffered)       │
                       │  cost_guard  ·  firebase_guard   │
                       └───────────────┬──────────────────┘
                                       ▼
                          Firestore  +  Cloud Storage
                                       ▲
                          Gemini  ·  Cloud Monitoring  ·  BigQuery
```

- **`bot.py`**: entry point. Defines `GeneKermanBot`, loads every cog,
  optionally syncs the command tree, and starts the Discord client and the
  uvicorn API server as concurrent asyncio tasks. It also contains the
  monkey-patch of discord.py's dispatch internals that powers the admin
  **mimic** system.
- **`config.py`**: the singleton `cfg`, loaded from `.env` at import time.
  `from config import cfg` everywhere; never read `os.environ` directly.
- **`settings.py`**: public, committable gameplay and balance values.
- **`i18n.py`**: two-tier localization: `t(guild_id, key)` for public/server
  messages, `tp(guild_id, user_id, key)` for ephemeral/personal ones. Both take
  `**kwargs` for placeholders.

  > The machinery is live but there is currently **one language**:
  > `SUPPORTED_LANGS = ("en",)`. Keep using `t`/`tp` for new strings anyway;
  > the per-guild and per-user lookup is what makes adding a language later a
  > data change rather than a code change.
- **`api_server.py`**: the FastAPI app (port 5022 by default), plus every
  request handler for the KSP client, the website, and the owner console.
- **`api_auth.py`**: the linking and session layer.
- **`contract_actions.py`**: the single implementation of every contract state
  transition, shared by Discord, the KSP client and the website.

Cogs share state through `store`, `settings` and `i18n` rather than through each
other. Direct cog-to-cog imports do exist where one cog genuinely owns a helper
another needs: `cogs.perms` (nearly everywhere), `contract_views` (imported by
`contracts` and `auctions`, that direction only), `gkchannels` (by `corps` and
`tickets`), `screenshots.active_client` (by `contract_views` and `api_server`),
plus helpers from `roles`, `moderation`, `corps`, `tickets` and `auctions`.
Prefer the shared modules for new work.

---

## Project Structure

```
GK Discord Bot/
├── bot.py                  # Entry point: cogs, command tree, both servers
├── config.py               # .env loader (the `cfg` singleton)
├── settings.py             # Gameplay/balance values, safe to commit
├── i18n.py                 # t() / tp() localization
├── command_group.py        # The shared /<group> parent, when COMMAND_GROUP is set
│
├── api_server.py           # FastAPI: KSP + website + owner console
├── api_auth.py             # Link codes, signed session tokens, device binding
├── api_models.py           # Pydantic request/response models
├── contract_actions.py     # Every contract state transition, in one place
│
├── cost_guard.py           # Three-tier spend meter and the enforcement ladder
├── orbit_render.py         # Stylized 2D orbit diagrams for submissions
├── flag_preview.py         # Watermarked preview of a flag-design submission
│
├── cogs/                   # Discord features (see below)
├── data/                   # Firestore integration and domain data
├── tools/gen_part_aliases.py   # Regenerates data/part_aliases.py from the mod
└── scripts/                # One-off maintenance/backfill scripts
```

### `cogs/`

Every entry is a loaded extension except `perms.py`, `targets.py` and
`contract_views.py`, which are helper modules living here because they belong to
this half of the codebase. `moderation.py` is loaded only when `ENABLE_MOD_COMMANDS` is set.

| Module | Role |
|--------|------|
| `admin.py` | Admin and owner commands: channel/role mapping, announcements, cog reload, mimic, version and policy publishing, `/costs` |
| `perms.py` | **Mimic-safe** permission helpers. Every gate must go through these |
| `targets.py` | Helper. Turns a `member:` **or** a `username:` into the account id a mod command spends: the only way to reach a player with no Discord |
| `economy.py` | KCoins: balance, pay, and the mod tools (give / fine / set) |
| `xp.py` | Levelling, rank, leaderboard, and the `auto_save` loop that flushes the store |
| `roles.py` | Self-assignable role menus |
| `corps.py` | Corporations: one per user, each with a dedicated channel, created when the player links KSP (never on server join) |
| `contracts.py` | Player-to-player contracts, Discord side (**creation lives in the mod and the website**) |
| `contract_views.py` | The interactive contract buttons and the Discord-side AI review |
| `contractcraft.py` | "Load to KSP" on a corp-delivered contract craft |
| `auctions.py` | Reverse (Dutch) auctions: escrowed start price, bids drive it *down* |
| `weeklymissions.py` | The weekly mission board, drawn from `data/mission_templates.py` |
| `screenshots.py` | `/analyze`: Gemini screenshot analysis; also owns `active_client()` |
| `tickets.py` | Private support / report / bug tickets |
| `moderation.py` | Kick, ban, mute, purge, warn |
| `gkchannels.py` | Gates bot commands to designated channels |
| `ksp_bridge.py` | `/linkcode`, `/privacy`, `/deletemydata`, and the persistent Link button |
| `costwatch.py` | Feeds the cost guard from Cloud Monitoring / BigQuery and warns the owner |
| `general.py`, `info.py` | Help, ping, server/user info |
| `marketplace.py` | **Tombstone.** Claims the three retired custom_ids and points at the website |

### `data/`

| Module | Role |
|--------|------|
| `store.py` | The `store` singleton: user data, in-memory write buffer, auto-save |
| `accounts.py` | Who a player *is*, separately from where they signed up: account ids, the Discord/Firebase indexes, usernames |
| `contracts.py` | Contract CRUD against Firestore |
| `marketplace.py` | Listings, votes, reports, the `recommended` sort |
| `auctions.py` | Auction documents |
| `guild_config.py` | Per-guild channel and role mapping (`/admin setchannel`, `/admin setrole`) |
| `imports.py` | The craft import / gift queue |
| `friends.py` | Mutual, guild-independent friendships. One document per player, both sides written in a transaction |
| `crew_ledger.py` | Which of a player's crew went to whom, so a lent crew can come home without being treated as impersonation |
| `craft_bans.py` | Hash bans on a single craft file, keyed by which of three fingerprints a ban names |
| `suspensions.py` | Timed API-surface suspensions (see [Security Notes](#security-notes)) |
| `suspicion.py` | Flags that feed moderation review |
| `cheat_check.py` | Server-side gate on client cheat reports |
| `tickets.py` | Ticket documents behind `cogs/tickets` |
| `twofa.py` | TOTP secrets and recovery codes |
| `mission_constraints.py` | Part-restriction extraction and verification, incl. `_TRAIT_MODS` |
| `orbit_constraints.py` | Orbital-regime extraction and verification |
| `mission_templates.py` | The authored pool the weekly board draws from |
| `part_resolver.py` | Resolves a loosely-typed part mention to a real installed part |
| `part_aliases.py` | **Generated**: see [Related Docs](#related-docs) |
| `celestial_bodies.py` | Visual and physical reference data for bodies |
| `telemetry_check.py` | Server-side plausibility check on submitted flight telemetry |
| `achievements.py` | Achievement definitions and awards |
| `mod_version.py` | The published DLL registry and the version gate's one acceptance decision |
| `policy.py` | The consent policy version the mod re-prompts on |
| `firebase_guard.py` | Transparent proxies that meter every Firestore/Storage call |
| `gcp_metrics.py`, `gcp_billing.py` | Cloud Monitoring and BigQuery billing readers |

---

## Slash Commands

Everything is a slash command. Admin commands sit under `/admin`, moderation
under `/mod`, and the rest at the top level (or under `COMMAND_GROUP`, if set).

| Group | Commands |
|-------|----------|
| **Economy** | `/balance` · `/pay` · `/richest` · `/givemoney`* · `/fine`* · `/setbalance`* |
| **XP** | `/rank` · `/leaderboard` · `/setxp`* |
| **Contracts** | `/contractreset`* · `/rescues` · `/rescueboard` |
| **Corps** | `/corpsetup` · `/corpsgenerate`* · `/corpsprivacy` |
| **KSP link** | `/linkcode` · `/privacy` · `/deletemydata` |
| **Screenshots** | `/analyze` |
| **Roles** | `/roles` · `/removeroles` |
| **Info** | `/help` · `/ping` · `/serverinfo` · `/userinfo` · `/botinfo` |
| **Tickets** | `/ticketpanel`* |
| **Missions** | `/add_custom_mission`* |
| **Mod** (`/mod …`) | `kick` · `ban` · `unban` · `mute` · `unmute` · `purge` · `warn` · `warnings` · `gkchannel` |
| **Admin** (`/admin …`) | `setchannel` · `setrole` · `announce` · `setprefix` · `linkas` · `publishversion` · `versioninfo` · `policyversion` · `costs` · `reload`† · `shutdown`† · `mimic`† · `unmimic`† |

\* moderator or admin only  ·  † bot owner only

### Reaching a player who has no Discord

A Boundless account does not need a Discord one (see
[Data & Persistence](#data--persistence)), so a moderator tool that could only take
a `discord.Member` could not touch a website sign-up at all, because there was no
snowflake to type. Every command that targets a player therefore takes **two**
optional fields and `cogs/targets.py` resolves either into an account id:

| Field | For |
|-------|-----|
| `member:` | anyone in this server: the normal case, picked from Discord's own list |
| `username:` | their permanent Boundless username, autocompleted from claimed names |

`/balance` · `/rank` · `/rescues` · `/givemoney` · `/fine` · `/setbalance` ·
`/setxp` · `/contractreset` all accept the pair. Naming **both** is refused
rather than resolved, and on the four that *write* naming neither is refused too
(on the read-only three it means "me"). A failed account lookup is refused as
"try again", never reported as "no such player". The distinction
`accounts.owner_of_username` exists to preserve.

The `member:` path is resolved through the same lookup rather than spending
`member.id`, which fixes a quieter bug: a player who linked Discord onto an
account they already had has a snowflake that maps *elsewhere*, so paying
`member.id` credited a wallet the game never reads.

---

## The KSP API

`api_server.py` serves `/api/v1/…` in-process with the bot. Every endpoint
requires a valid signed session token except the link handshake and `/health`.

### Linking

1. The player runs `/linkcode` in Discord → a one-time 6-digit code is written
   to Firestore with a **3-minute** lifetime (`LINK_CODE_LIFETIME`).
2. The KSP client `POST`s it to `/api/v1/auth/link`.
3. The server validates it and returns a **30-day HMAC-SHA256 signed session
   token** (`TOKEN_LIFETIME`).
4. The client sends that token on every request as `Authorization: Bearer …`.

No API key, Firebase credential or other secret is ever sent to a client.

### Endpoint groups

| Prefix | What it covers |
|--------|----------------|
| `/api/v1/auth/…` | Link, poll, verify, device list/remove, WS ticket, `logout_all`, suspension check |
| `/api/v1/version/check`, `/api/v1/attest/…` | The update gate, the policy version, and DLL challenge-response attestation |
| `/api/v1/contracts/…` | Create (incl. `create_rescue`), incoming, active, accept, submit, review, dispute, give up, settle, more-time |
| `/api/v1/craft/…` | Download, send, the gift offer inbox (accept / reject), and the import queue |
| `/api/v1/missions/…` | The weekly board and mission selection |
| `/api/v1/marketplace/…` | List and delist (the **selling** half: browsing lives on the website) |
| `/api/v1/auctions/create` | Auction creation |
| `/api/v1/user/…` | Profile and the notification feed (mark read, dismiss) |
| `/api/v1/parts/catalog` | Part catalog upload, so the bot can resolve fuzzy part mentions |
| `/api/v1/checkpoint-photo`, `/achievement-photo` | Cinematic milestone captures |
| `/api/v1/bugreport` | See below |
| `/ws/v1/notifications` | The WebSocket push channel (ticket-authenticated) |

### Bug reports

`POST /api/v1/bugreport` takes a summary, details and optionally the player's
`KSP.log` from the mod's Tools tab, and opens a normal ticket via
`cogs/tickets.create_ticket`, but with `ping_mods=False` and
`notify_role_key="bug_report"`. A bug is not a moderation matter, so it pings the
`bug_report` role (`BUG_REPORT_ROLE_ID`, or mapped per guild with
`/admin setrole`); left unset it falls back to pinging the mods rather than going
unread. That role can also close the ticket.

The log is trimmed to **head 2 MB + tail 7 MB on the client**
(`DeviceId.GetKspLogCapped`) so a 300 MB modded log is never uploaded whole;
`_trim_log` on the server cuts to the same shape as a backstop.

---

## The Website API

The website talks to the same process over `/api/v1/web/…`, with its own link
handshake (`/web/auth/link`) issuing the same kind of token.

| Prefix | What it covers |
|--------|----------------|
| `/web/marketplace/…` | Browse, buy, vote, report, relist, delist, delete, purchases, compatibility |
| `/web/contracts/…` | The same transitions as the KSP path, through `contract_actions` |
| `/web/auctions/…` | Bid and end (creation is KSP-only) |
| `/web/profile` | Profile |
| `/web/game/command` | Queue an action for the player's running KSP client |

### Marketplace votes & reports

All three actions are authenticated (`get_user_token_only`), because an anonymous like
is worth nothing and an anonymous report costs a moderator a channel.

- A vote is a **tri-state the client sends** (`VOTE_UP` / `VOTE_DOWN` /
  `VOTE_NONE`), never a "flip it". Toggle-on-second-press is a UI convention
  living only in the website's `listing-actions.tsx`, so a double-submit cannot
  undo a vote the user meant to keep.
- Storage answers the two questions the UI actually asks. "Which of these 25
  crafts have I voted on?" is **one** document read
  (`marketplace_votes/{user_id}` holds the whole map, rather than a doc per pair
  needing 25 reads or a collection-group index). The per-card tallies are stored
  counters moved by `firestore.Increment`, not sums. `set_vote` is the only
  writer of either, and undoes its increment if the user's own record fails to
  save.
- Sorting adds `likes` (net, all-time) and `recommended`, which is a *discovery*
  sort: net likes per day for listings under `RECOMMENDED_WINDOW_DAYS` (15), then
  everything else by net likes as a tail so a quiet fortnight doesn't empty the
  tab. The window is what makes it work: by likes alone, a year-old listing
  outranks a good one from Tuesday.
- A report opens a ticket in the **reporter's** guild (a ticket they cannot see
  is no use to them; the listing's origin server is named in the embed instead)
  with the seller as `subject_user_id`, shown to mods and deliberately not given
  access. The report doc id is the (listing, reporter) pair, so "already
  reported" is a keyed read and the same complaint can't be filed twice to look
  louder. `report_count` is merged into the owner console's rows only, never into
  the public listing.

### Craft compatibility pre-flight

Listings record the craft's exact part names (`parts`, sent by the KSP client
alongside `mods`), which `_craft_compatibility` checks against the buyer's
uploaded part catalog. Served by
`GET /api/v1/web/marketplace/{id}/compatibility` and returned on the web buy
result.

Parts the mod will **substitute** on install are reported separately and do *not*
count as incompatible, because warning about a problem that fixes itself on arrival is a
false alarm. `known=False` (no catalog uploaded yet, or a listing predating part
tagging) is deliberately distinct from compatible and must never render as a
green light. The whole thing is advisory, never a gate.

---

## Owner & Admin Console

`/api/v1/web/admin/*` is the website's developer master console: listings
moderation, user accounts (balance/XP set, revoke sessions, suspend/lift,
delete), DM-from-bot and announcements, remote channel lock/unlock, DLL
publishing, cost readouts and runtime gates.

It is gated in **two tiers**, mirroring `cogs/perms.py`:

| Dependency | Who | What it opens |
|------------|-----|---------------|
| `get_owner` | Only the `BOT_OWNER_ID` account: the same single owner as `cogs/perms.is_owner_user` | Everything bot-wide: user accounts, DM-from-bot, DLL publishing, costs, runtime gates, policy |
| `get_admin` | The owner, **or** a holder of a guild's mapped bot-admin role (key `"admin"` via `/admin setrole`) | The guild-scoped moderation surface: overview, listings whose origin `guild_id` is a guild they admin, announcements, channel locks, guild pickers, with every response and action cut to their guilds |

Guild administrators are **not** auto-admins. A role granted in one guild must
never carry authority over every guild the bot is in.

Both tiers answer **404**, not 403, to everyone else. The surface is invisible.
Admin-role membership is resolved live from the Discord member cache on every
request, so revoking the role revokes console access immediately.

The website's `/admin` tab is drawn for whoever `whoami` accepts, with the
owner-only tabs (Users, Mod Version, Costs, Controls) and the DM card hidden from
guild admins, but that visibility is **presentation only**; the API dependency
is the real gate.

> Runtime gate flips are **process-local**. `.env` is the boot-time truth, and a
> restart reverts them.

---

## Data & Persistence

**User data is global.** XP, balance, message counts, unlocked levels and the
language preference live at the **top-level, guild-independent** `users/{user_id}`,
mirrored in an in-memory dict by `data/store.py`. The wallet spans every server:
`store.get_user(guild_id, user_id)` still takes a `guild_id` for call-site
compatibility but ignores it as a key.

**A user id is an account id**, not necessarily a Discord snowflake.
`data/accounts.py` makes a Discord-origin account's id *be* its snowflake and a
website sign-up's id `a_<firebase_uid>`, so every existing document keys the same
way and a player with no Discord still has somewhere to live. Two consequences
for anything that touches a store key: never `int()` it (use
`accounts.is_discord_account`, or `cogs/targets.board_name` for a leaderboard
row), and never assume there is a Discord user behind it to mention or DM.

**Guild-scoped state** (config, corps, weekly missions and selections,
tickets) lives under `guilds/{guild_id}/…`. Contracts and notifications are
stored directly in Firestore too.

**Writes are buffered.** `store.save_if_dirty()` is driven by the `auto_save`
loop in `cogs/xp.py` every `settings.AUTO_SAVE_INTERVAL` seconds (300). The flush
goes to **Firestore only**. `settings.DATA_FILE` (`data/users.json`) is a
leftover of the pre-Firestore store that nothing reads or writes any more.

All Firestore operations are **synchronous** (the firebase-admin SDK), while
Discord and API handlers are async. Use `await store.add_balance(...)` for async
write helpers; reads like `store.get_user(...)` are plain synchronous calls.

---

## Contracts

`contract_actions.py` is the one implementation of every contract state
transition: accept, submit, review, dispute, give up, settle, more-time. Discord
views, the KSP endpoints and the website endpoints are all thin callers. That
matters because the same transition used to exist in three places and had already
drifted.

Craft compatibility, mission limits and orbit requirements are checked at three
points: in the editor (mod-side `EditorPartEnforcer`), at submit time on the
client, and authoritatively on `/submit` here.

**Crew professions** (`crew_traits`) are matched by the exact
`ProtoCrewMember.trait` string on both ends, which is what lets a contract written
on a modded install still mean something on one without it. Which *mod* defines a
modded profession is the one thing that string cannot express and no part walk
can recover, so it is written down twice and kept in sync by comment:
`data/mission_constraints.py::_TRAIT_MODS` and the mod's
`ContractConstraints.cs::TraitMods` (the same convention as `ENGINE_CATEGORIES` ↔
`PartClassifier.GetEngineCategories`).

Both tables are **closed**: an unlisted profession yields no mod name rather
than a guessed one. Only a *floor* names its mod: a ceiling ("no Kolonists") is
satisfied by not having the mod, so naming it would read as advice to install
something in order to obey a ban.

---

## The Economy: Debt, Garnishment and the Ledger

### Fines follow the player

Every fine in the system is collected through one function, `_charge_fine` in
`contract_actions.py`. It takes what the contractor has and records the rest as a
**debt to the issuer**, repaid out of a share of later earnings.

Before this, a fine was collected with `try_debit` on the two voluntary paths and
`debit_up_to` on the two involuntary ones, and whatever the contractor could not
cover was silently forgiven. That made the penalty proportional to the offender's
wealth, smallest for exactly the players most likely to walk away, and a
zero-balance player judgment-proof. Worse, the honest exit was the only blocked
one: Give Up and Pay Fine refused a contractor who was short, while submitting
junk and waiting out the dispute clock took the same partial amount three days
later.

Five things are load-bearing:

- **Earnings are opt-in** (`store.add_balance(garnishable=True)`), never a blanket
  hook. Roughly half the credits in this codebase are refunds and corrections:
  auction escrow coming back, a marketplace double-buy refund, an owner-console
  fix, and `/fine` itself, which passes a negative amount. Skimming those would
  confiscate a player's own money, so the default is off and a call site nobody
  flagged repays a debt slower rather than stealing.
- **The rate scales with the amount owed, not the creditor count.** Owing two
  people a little is not worse than owing one person a lot, and a count-based
  rate is gameable from both ends.
- **The split is pro-rata with a largest-remainder rule**, because the wallet is
  an integer and without it a debt strands at a coin or two.
- **The ledger lives on `users/{user_id}`**, so the debt and the balance it is
  collected from are one document and one flush.
- **It is said out loud.** `/balance`, the sidebar's Profile panel, the website
  account page and every refusal sentence name the debt and the rate.

Garnishment rather than a lockout, because a lockout punishes but never collects
and leaves no way back. `DEBT_MAX_OUTSTANDING` is the one gate kept, and it stops
*accepting* new contracts rather than earning.

### The transaction ledger

The wallet used to be a single number, which answers "how much have I got" and
nothing else. Every movement is now written down at the five functions that can
move a balance, which is the whole surface, so a new call site is metered by
construction and a missing `category=` is recorded as `other` rather than dropped.

It lives on the user document as a capped list rather than in a subcollection,
because `store` already re-serialises the whole record on every flush: the ledger
rides a write that was happening anyway and costs **no additional Firestore
operation**. The list is a ring buffer and the totals are not, so a summary is
never computed by summing visible rows. The recorded amount is the delta that
actually landed, read off the balance either side of the change, because
`add_balance` clamps at zero and a ledger that recorded the *request* would fail
to add up to the balance it claims to explain.

Served by `/api/v1/finance` to the mod's Finance panel, with names resolved at
read time rather than stored, since a display name changes and a baked-in one
would be wrong forever. Escrow is deliberately **not** in the ledger and is
derived from the contracts themselves by `api_server._escrow_held`.

---

## Friends, Quicksend and the Crew Ledger

### Friends

A friendship is mutual and explicit (request, then accept), keyed on **account
ids**, so a Boundless account with no Discord is a first-class friend. It is
deliberately **guild-independent**: it is between two people, not between two
people in a server.

The recipient list for a craft quicksend used to be everyone with a corp in the
caller's guild, which made the counterparty of a hand-over anybody at all in a
large Discord.

- Storage is **one document per player, not per pair**, so opening the picker is
  one read and "may I send to this person" is one read.
- Every mutation writes **both** documents in a transaction, because half an
  accept is the one state that must never exist. The write is a whole-document
  `set` with no merge, since `merge=True` deep-merges nested maps and would make
  every unfriend a no-op.
- Reads fail **closed**, unlike suspensions and craft bans, because this decides
  who receives somebody's ship.
- **The gate is the server** (`friends_db.are_friends` in `/api/v1/craft/send`),
  not the picker, so a client drawing the wrong list cannot become a send to a
  stranger.

Not extended to contract creation, `/pay` or Send coins: those move money, which
is not a hand-over of anything that can be lost.

### Quicksend and gifts

A blueprint send is a copy. A **live-vessel send is a hand-over**: once the
server confirms, the sender's client removes the vessel and its crew from the
save, and a decline gives it back by re-queuing the stored snapshot to the sender
rather than deleting it. Three fields make that safe across versions and
rollbacks: `vessel_pid`, `vessel_returnable` and the pid echoed back on accept.

### The crew hand-over ledger

`VesselTransfer.ApplyIncomingOwnershipTag` refuses an incoming crew name that
claims to be ours, because the crew node is written entirely by the sender. An
honest **return leg has exactly that forgery's shape**, so lending a crewed ship
to a friend and getting it back used to return the kerbals double-tagged and
borrowed, with the originals gone from the roster.

`data/crew_ledger.py` is the missing record. `/api/v1/craft/send` writes down
which of the sender's crew left and to whom, and on a live-vessel send **back to
that same person** offers exactly those names as `homebound`. Only bare names are
recorded, attestation is per (owner, holder) pair rather than per owner, entries
are expired rather than consumed, and the read fails **open** so a Firestore blip
cannot become a refused hand-over.

---

## Moderation: Craft Bans, Reports and Suspensions

### Craft hash bans

The moderation surface already answered "this listing is bad" and "this player is
bad". This answers the case in between: one *file* that must not circulate.
Delisting removes a listing, not the craft, which is still on the uploader's disk
and returns under a new name, from a new account, in another guild.

So the key is a hash of the craft, and the design is about **which** hash. Every
upload is fingerprinted three ways and a ban names the one it enforces:

| Kind | What it hashes | Why |
|------|----------------|-----|
| `exact` | The bytes | No false positives, and almost no reach, since the export chain appends side-channel blocks and a fresh thumbnail |
| `design` | Every part's base name and its position rounded to the centimetre, sorted | Survives a rename, a re-description and a re-export, which is how a craft usually comes back |
| `parts` | The part names alone | Catches the craft that was banned and then had one part nudged. It can over-match, so the dialog shows how many listings it would take down *before* the ban is issued |

Enforced on every path that takes a craft from a client and hands it to somebody
else: marketplace listing, quicksend, contract submission, plus relist. Issuing a
rescue and the vessel node of a rescue submission are deliberately exempt, being a
player's own broken ship going out and coming home. Reads fail **open**: a craft
ban is nuisance control, and no failure of it is worth refusing every upload in
the game over. A `.craft` is plain text, so anyone willing to open one in an
editor gets past any hash of it, and nothing downstream may treat "got past the
gate" as "safe". Owner-only, because a hash is the same hash in every guild.

### Contract reports

The marketplace's report system points at a craft. This one points at a *person*:
the counterparty of a contract. Same shape, because the question a moderator asks
is the same one, with four differences:

- **Only the two parties may file one.** A listing is public, a contract is
  private, so a stranger asking about one is answered 404 rather than told it
  exists.
- **A bot-issued contract is refused.** A weekly mission has no human on the other
  side, so there is nobody for a moderator to talk to.
- **The status is stored with the report**, because a report is about a moment and
  the live document will have moved on before a moderator opens the ticket.
- It is **not** in `contract_actions`, because a report is not a state transition
  and changes nothing about the deal.

Sue and report must not be confused: `ca.dispute(action="sue")` asks a moderator
to decide a *contract*, this asks about a *person*. That is why report is offered
in **every** status while the dispute buttons are not.

---

## The Mod Version Gate

`data/mod_version.py` holds the published DLL registry: which builds exist, their
SHA256, and which one is current. A client reports its own DLL's hash and the
server decides whether it may talk to this server at all.

### The grace window

The gate forces a player onto the current build, and CKAN is the update route the
project recommends. Those two do not compose on their own: NetKAN indexes a
release on its own schedule and the player still has to open CKAN, so between a
publish and an upgrade being *offered* there is a lag nobody here controls or
observes. Gating on the latest hash alone turns that lag into a lockout, and makes
a tool this project does not run a hard dependency of every login.

So a build that was the published latest until recently is **accepted and told it
is out of date**, for `settings.MOD_VERSION_GRACE_DAYS` (7 by default).

- The window is measured from when a build **stopped** being latest
  (`superseded_at`, stamped on the outgoing entry at the one moment
  `publish_version` can observe the transition), not from its own publish date.
  That is when the player's copy actually went stale, and it chains correctly: on
  a rapid A to B to C, A ages out on B's clock, so a release today cannot revive a
  build that went stale a month ago.
- Grace is only ever extended to **a hash this server published**. An unknown
  hash, which is exactly what a modified DLL is, is refused however wide the
  window, so the window is not a hole in the tamper gate.
- There is **one decision, not two**. `mver.acceptance` is shared by
  `check()` and `enforce_mod_version`, because a client told at startup that it
  may proceed and then refused 426 by every call it makes presents as the mod
  being *broken* rather than as out of date.
- `up_to_date` answers **"may I proceed", not "am I newest"**, and a graced client
  gets `true`. Every client already in the wild treats `false` as "raise the
  blocking window" and knows nothing about grace, so the field every client
  already obeys has to carry the decision. The two literal questions are answered
  by `on_latest` and `update_available` beside it.
- A missing or unparseable `superseded_at` means **no grace**, falling back to the
  strict gate.

Runtime-adjustable from the console's Controls tab (`mod_version_grace_days`),
read fresh on every decision, because the moment it needs widening is an incident.
`0` restores the strict latest-hash-only gate.

Covered offline by `test_mod_version_grace.py` (see [Tests](#tests)).

---

## AI Integration

Gemini (`google-genai`, one model, `_MODEL` in `cogs/screenshots.py`) is called
from **six** places:

| Call site | What it does |
|-----------|--------------|
| `cogs/screenshots.py::_run_gemini` | Screenshot analysis: images in, craft/location/difficulty JSON out |
| `cogs/contract_views.py` | Reviews a Discord-issued contract submission (mission text + screenshots) |
| `api_server.py::_ai_review_submission` | The same review for an in-game submission, plus vessel telemetry and loadmeta |
| `api_server.py::_classify_single_contract` | One contract's mission text → `craft_build` vs `active_vessel`, situation, body, `constraints` |
| `api_server.py::_classify_missions` | The same for a whole week's missions at once |
| `api_server.py::_ai_resolve_part` | Pins a loosely-named part in a mission text to an installed one |

**Every call site goes through `active_client()`**, which returns `None` when
`GEMINI_API_KEY` is unset **or** the monthly `cost_guard` budget is spent, so a
blown budget degrades exactly like a missing key. Each site has a fallback:
screenshot analysis is disabled, classification and constraint extraction fall
back to the keyword heuristics in `data/mission_constraints.py`, and a submission
review auto-accepts.

Weekly missions are **not** AI-generated. `cogs/weeklymissions.py` draws them
from `data/mission_templates.py`, and their descriptions are authored by an admin
command; AI only *classifies* them. Classification results are cached in
Firestore (`mission_classifications`) so AI runs at most once per week per set of
missions.

**On language.** Anything Gemini *reads* is language-agnostic: a mission can be
written in any language and still be classified. Two things are not: the
heuristic fallbacks (`extract_heuristic`, `_classify_text_heuristic`,
`orbit_constraints`) match hardcoded English + Turkish keyword tables, and
screenshot analysis pins its text fields to English on purpose. The Turkish
tables are **not dead code** even though the bot's own strings are English-only:
the crew and crew-profession heuristic runs on every classification to fill
bounds the AI left unset, not only when the AI is unavailable. Of everything
Gemini returns, the only free text is a review's `reason`.

---

## Cost Guard

`cost_guard.py` tracks what Gemini and Firebase cost each month, and brakes
before the bill does. **Three tiers**, because no single source is both fast
enough to brake on and accurate enough to trust.

| Tier | Source | Latency | Role |
|------|--------|---------|------|
| **0** | `data/firebase_guard.py`, transparent proxies counting every op and byte as `store`'s `_db` / `_storage_bucket` handles are used | instant | **The trigger.** Wrapping those two handles meters all 16 call-site modules at once |
| **1** | `data/gcp_metrics.py`, Cloud Monitoring | minutes | **The truth.** Adopted as a baseline by `ingest_usage`; tier 0 keeps counting on top and the gap is kept and reported as `drift` |
| **2** | `data/gcp_billing.py`, the BigQuery billing export | hours | **Display only.** Actual billed dollars, net of free-tier credits |

Tier 0 is instant, which is the only property a brake needs, and it cannot be
right. A signed or public URL is fetched straight from GCS, so those bytes never
pass through the process. That is not a small gap: measured on this project it
was **773 KB seen against 469 MB actual**. Tier 1 sees that egress, the bytes at
rest, and any usage that isn't the bot at all (scripts, the console).

Tier 2 is deliberately never read by `_level_locked`: a brake fed by a source
that lands a few times a day would let a runaway spend for a whole export cycle
first. Its value is being right *without any modelling*: the free tier arrives
as negative `credits` rows, so `cost + credits` is the invoice rather than a
guess at one, and where it disagrees with tier 1 the error is in our own price
constants.

Both GCP readers call the REST APIs with `google-auth` + `aiohttp` (both already
present) instead of `google-cloud-monitoring` / `google-cloud-bigquery`, so they
add **no dependency**. Each needs an IAM grant the Firebase key lacks by default:
`roles/monitoring.viewer` on the project for tier 1, and
`roles/bigquery.dataViewer` on the dataset plus `roles/bigquery.jobUser` on the
project for tier 2. Without them every call 403s and the guard simply runs on the
tiers it does have.

### The enforcement ladder

```
NORMAL → WARN → DEGRADED → FROZEN
                    │          └─ require_firebase raises FirebaseBudgetExceeded
                    │             on everything
                    └─ Storage *uploads* refused; reads, downloads and Firestore
                       keep working, so the bot stays usable
```

Freezing arms exactly one `final_flush` grace pass, because flushing `store`'s
memory buffer is itself a Firestore write, and a stop that refuses it converts
"we stopped spending" into "we lost everyone's last few minutes of XP".

The free tier is modelled **per day and in US/Pacific** (`FREE_TIER_TZ`), which
is how Google actually applies it. Charging from operation #1, as the first
version did, reported $0.0059 on a $0.00 bill.

State is **local only** (`data/cost_state.json`, archived to
`data/cost_history.jsonl` on rollover): the meter must survive Firebase being the
thing cut off, and must not itself cost Firestore reads. Levels are announced to
the owner by `cogs/costwatch.py`, which drains a queue rather than being called
directly, because the guard runs on whatever thread firebase-admin is on, which is no
place to touch Discord. Surfaced by `/costs` and the console's Costs tab; budgets
and the guard switch are runtime-flippable, since the failure worth planning for
is a *false* stop from wrong price constants.

---

## Security Notes

- `.env` and the Firebase credentials are **never** committed. Check
  `.gitignore` before adding either.
- All persistent data lives in Firestore; nothing sensitive is kept on disk
  besides the cost meter's own state.
- **No secret ever reaches a client.** The KSP mod and the website hold only a
  signed session token.
- Session tokens are HMAC-SHA256 signed. `API_SECRET_KEY_PREVIOUS` exists so the
  key can be rotated without invalidating every live session at once.
- **Mimic safety**: the admin mimic system patches three internal discord.py
  dispatch points so `interaction.user` is swapped before any handler sees it.
  Any code that needs the *real* user must read
  `interaction.extras.get("_mimic_real_user")`, and every permission check must
  go through `cogs/perms.py`, or an admin mimicking the owner would borrow the
  owner's authority.
- **Runtime gates** (`KSP_VERSION_CHECK_ENABLED`, `KSP_DEVICE_BINDING_ENABLED`,
  `KSP_2FA_ENABLED`, `API_DOCS_ENABLED`, `DEBUG_ENDPOINTS_ENABLED`) can be
  flipped from the console, but `.env` is the boot-time truth.

### Service suspensions

A suspension (`data/suspensions.py` + `enforce_not_suspended`) is a **timed block
on the API surface** (the KSP client and the website), issued from the console's
Users tab and enforced in `get_user_token_only`, so every token-gated endpoint is
covered by one check.

It is deliberately **not** a Discord ban (`cogs/moderation.py` owns those, and
they act on guild membership) and **not** a wipe: balance, XP, contracts and
listings are untouched and waiting. There is no permanent option, and `MAX_HOURS`
caps one at a year, because a suspension that never ends is a ban wearing a
disguise.

Refusal is `403 {"code": "suspended", reason, until}`, structured like the device
gate so the client can *draw* it. Sessions are deliberately **not** revoked: a
revoked token drops the mod to its link screen, whose only offer (link again)
would work and change nothing, whereas a live token means every request comes
back carrying the explanation.

Three endpoints are exempt, via `get_user_allow_suspended`:

- `/api/v1/auth/suspension`: the "check again" read, the only endpoint that
  answers a suspended client normally, so a *lifted* suspension has something to
  report
- `/api/v1/auth/logout_all`: the user's own privacy control; a punishment must
  not take it away
- the Discord-side data purge, which was never an API endpoint

Expiry is resolved **on read** (`until > now`, no sweeper): on the server, in
the 30-second in-process cache (clamped so an entry never outlives the suspension
it describes), and in the mod's own `Update`. A Firestore read failure fails
**open**: an outage that suspended everyone would be far worse than a suspended
player getting extra minutes.

---

## What Discord No Longer Does

Three surfaces were retired from the bot, and each left something behind on
purpose.

**Contract creation** (`/contract`, `/flagcontract`) is gone. A contract is
written in the mod's `ContractForm` or on the website, the only places that can
read the craft, the mod list, and the orbit/Δv margins the mission is later
judged against.

**Contract submission** went with it, bar one case: `SubmitButton` still serves a
`flag_design` contract, because a flag genuinely *is* just an image handed over,
and Discord is the only submission path it has ever had (there is no in-game
upload for one, and `_submit_flag` in `cogs/contract_views.py` is the sole writer
of `flag_fullres_url` / `flag_preview_url`). Everything else answers with
`SUBMIT_MOVED_NOTICE` rather than a dead interaction, since work views posted to
corp channels outlive the code that drew them, so `ContractWorkView` takes a
`mission_type` and only draws Submit for a flag. The AI auto-review that used to
live in that view is not lost: `api_server._ai_review_submission` /
`_auto_accept_contract` are the same thing on the in-game path, and the weekly
mission card says so (`weeklymissions.SUBMIT_IN_KSP`).

**The marketplace** is gone entirely: no mirrored listings, no Buy/Delist/Load
buttons, no `/market` or `/delist`, no `marketplace` channel key, and
`POST /api/v1/marketplace/list` no longer requires a Discord channel to exist
before it will accept a listing. `cogs/marketplace.py` survives as a
**tombstone** that claims the three old custom_ids (`mk_buy`, `mk_delist`,
`mk_load`) and answers them with a pointer to the website: the mirrored messages
are still in people's channels, and Discord renders an unclaimed custom_id as
"This interaction failed", which reads as *broken* rather than *moved*. The
`mirrors` field on a listing is kept for shape only; nothing writes or reads it
now.

**Auctions are untouched** and still live in Discord, because a bidding game between
people is the one thing a channel does better than a game client. (Creation moved
to the mod; bidding and ending are Discord and the website.)

---

## Tests

**The offline suites are not in this repo.** They are maintained alongside the
three checkouts in the maintainer's working tree, because most of them import the
bot *and read its own source text* to assert on it, which only resolves against a
full local layout. Nothing here runs them, and no test file is tracked.

They are standalone scripts rather than pytest: each runs itself and exits
non-zero on failure, so they need no runner and no plugins. All of them are
offline, with no network, no Firebase operations and no KSP.

What they hold in place, so the guarded invariants are on record:

| Suite | Covers |
|-------|--------|
| `test_cost_guard.py` | The three tiers, the free-tier day model, the enforcement ladder, the final-flush grace pass |
| `test_contract_actions.py` | Every contract state transition |
| `test_debt.py`, `test_finance.py` | Fine debt, garnishment order and the transaction ledger |
| `test_friends.py`, `test_crew_ledger.py` | Mutual friendship, and the crew hand-over attestation |
| `test_craft_bans.py` | The three fingerprint kinds and the listing sweep |
| `test_suspensions.py` | The gate, the exemptions, expiry-on-read, the bounded maximum |
| `test_mod_version_grace.py` | The version gate's grace window, its stamping and its off switch |
| `test_security_signing.py` | Signed-URL behaviour, with real V4 signing done locally |
| `test_security_invariants.py` | Source invariants and the sanitizer algorithm spec |
| `test_auth_hardening.py` | Key rotation, device-gate fail-open, sweep defence |
| `test_targets.py` | The moderator target resolver: username lookup, rebound snowflakes, and every refusal around them |
| `test_accounts.py`, `test_twofa.py`, `test_tickets.py` | Account identity, TOTP, ticket creation |

Several dated audit suites sit beside them, where a non-zero exit is a
*reproduced finding* rather than a regression.

---

## Related Docs

| Doc | Where | What it is |
|-----|-------|------------|
| [KSP mod](https://github.com/Boundless-Missions/boundlessmissions-modside) | separate repo | The other half of this project: the in-game client this server exists to serve |
| [Website](https://github.com/Boundless-Missions/boundlessmissions-website) | separate repo | The marketplace, contracts and account pages, and the owner console |
| `CLAUDE.md` | not published | Architectural rationale for both components. Kept in the maintainer's working tree, not in any repo |
| `DEV-SETUP.md` | not published | Toolchain state, project structure, day-to-day commands |
| `DEPLOYMENT.md` | not published | Production VPS runbook (systemd, Caddy, secrets, health checks). Deliberately unpublished: it describes live infrastructure |

**`data/part_aliases.py` is generated**, not authored. Its source of truth is
`PartAliases.cs` in the KSP mod; regenerate it with
`python tools/gen_part_aliases.py` whenever that table changes.
