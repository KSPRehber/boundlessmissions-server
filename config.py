"""
config.py – Centralised environment-variable loader.
All secrets are read from the .env file (or the real environment).
Import `cfg` anywhere in the project to access settings.
"""

import os
import logging
from dotenv import load_dotenv

# Load .env from the project root (one directory up from this file)
load_dotenv()


def _require(key: str) -> str:
    """Read a required env var; raise if missing."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Check your .env file against .env.example"
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


class Config:
    # ── Discord credentials ─────────────────────
    TOKEN: str = _require("DISCORD_TOKEN")
    CLIENT_ID: str = _optional("DISCORD_CLIENT_ID")
    CLIENT_SECRET: str = _optional("DISCORD_CLIENT_SECRET")

    # ── Guild IDs for dev slash-command sync ───
    # e.g. "123456789,987654321"  → [123456789, 987654321]
    _raw_guilds = _optional("GUILD_IDS", "")
    GUILD_IDS: list[int] = (
        [int(g.strip()) for g in _raw_guilds.split(",") if g.strip()]
        if _raw_guilds
        else []
    )

    # ── General settings ────────────────────────
    COMMAND_PREFIX: str = _optional("COMMAND_PREFIX", "!")
    OWNER_ID: int = int(_optional("BOT_OWNER_ID", "0") or "0")

    # ── Slash command group ──────────────────────
    # If set, all slash commands live under this group name.
    # e.g. COMMAND_GROUP=gk  →  /gk help, /gk ping, /gk kick …
    # Leave blank to keep bare top-level commands (/help, /ping …)
    COMMAND_GROUP: str = _optional("COMMAND_GROUP", "")

    # ── Feature flags ───────────────────────────
    # Set to "false" (case-insensitive) to disable the moderation cog entirely
    ENABLE_MOD_COMMANDS: bool = _optional("ENABLE_MOD_COMMANDS", "true").lower() not in ("false", "0", "no", "off")

    # ── KSP API Server ──────────────────────────
    KSP_API_ENABLED: bool = _optional("KSP_API_ENABLED", "true").lower() not in ("false", "0", "no", "off")
    API_HOST: str = _optional("API_HOST", "0.0.0.0")
    API_PORT: int = int(_optional("API_PORT", "5850"))
    API_SECRET_KEY: str = _optional("API_SECRET_KEY", "gk-change-this-secret-key")

    # ── Firebase / Firestore ────────────────────
    # Path to the Firebase service account JSON key file
    FIREBASE_CREDENTIALS: str = _require("FIREBASE_CREDENTIALS")

    # ── Logging ─────────────────────────────────
    LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()


cfg = Config()

# Configure root logger once here so every module inherits it
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
