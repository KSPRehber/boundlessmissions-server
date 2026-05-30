# Discord Bot – genekerman

A modular, production-ready Discord bot built with **discord.py 2.x**.

## Features

| Category | Commands |
|---|---|
| **General** | `/help`, `/ping` |
| **Info** | `/serverinfo`, `/userinfo`, `/botinfo` |
| **Admin** | `/announce`, `/reload`, `/shutdown`, `/setprefix` |
| **Moderation** | `/kick`, `/ban`, `/unban`, `/mute`, `/unmute`, `/purge`, `/warn`, `/warnings` |

All commands are available as **slash commands** (`/`) and select ones also as prefix commands.

---

## Quick Start

### 1. Clone / open the project

```bash
cd /home/ayd/Desktop/genekerman
```

### 2. Create a virtual environment & install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
nano .env   # fill in your values
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from [Discord Developer Portal](https://discord.com/developers/applications) |
| `DISCORD_CLIENT_ID` | ☑️ | Application / client ID |
| `DISCORD_CLIENT_SECRET` | ☑️ | Client secret (for OAuth flows) |
| `GUILD_IDS` | ☑️ | Comma-separated guild IDs for fast dev slash-sync (leave blank = global) |
| `COMMAND_PREFIX` | ☑️ | Prefix for text commands (default `!`) |
| `BOT_OWNER_ID` | ☑️ | Your Discord user ID (unlocks owner-only commands) |
| `LOG_LEVEL` | ☑️ | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 4. Create the bot & invite it

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. **Bot** tab → **Add Bot** → copy the token into `.env`
3. **Bot** tab → enable all **Privileged Gateway Intents** (Presence, Server Members, Message Content)
4. **OAuth2 → URL Generator**: scopes = `bot` + `applications.commands`, permissions = **Administrator**
5. Open the generated URL to invite the bot to your server

### 5. Run the bot

```bash
python bot.py
```

---

## Project Structure

```
genekerman/
├── bot.py              # Entry point: loads cogs, syncs commands, starts bot
├── config.py           # Env-var loader (import cfg anywhere)
├── requirements.txt
├── .env.example        # Template – copy to .env
├── .env                # Your secrets (gitignored)
└── cogs/
    ├── general.py      # help, ping
    ├── admin.py        # announce, reload, shutdown, setprefix
    ├── moderation.py   # kick, ban, unban, mute, unmute, purge, warn, warnings
    └── info.py         # serverinfo, userinfo, botinfo
```

## Adding a New Cog

1. Create `cogs/mycog.py` with a `setup(bot)` async function
2. Add `"cogs.mycog"` to the `cog_modules` list in `bot.py`

---

## Security Notes

- `.env` is **never** committed — add it to `.gitignore`
- All admin/mod commands are permission-gated server-side
- Warnings are stored in memory; restart clears them — add a DB for persistence
