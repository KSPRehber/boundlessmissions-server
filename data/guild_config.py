"""
data/guild_config.py – Per-guild channel & role configuration.

Historically every functional channel (auctions, marketplace, weekly missions,
tickets, …) and every assignable role (the 15 KSP "Level" achievement roles, the
mod role) was a single hardcoded ID in settings.py. Those IDs only make sense in
one "home" server: discord.py resolves channels/roles by *global* ID, so running
the bot in a second guild would post into the wrong server or fail.

This module stores those mappings PER GUILD in the existing `guilds/{guild_id}`
Firestore document (the same doc gkchannels.py writes `gk_channels` into), so the
bot can operate across many servers, each with its own channels and role IDs.

Firestore layout (merged into guilds/{gid}):
    config_channels: { "<channel_key>": <channel_id_int>, ... }
    config_roles:    { "<role_key>":    <role_id_int>,    ... }

settings.py values remain as FALLBACK defaults for the home guild only — a default
is used only when `resolve_channel`/`resolve_role` confirms the target actually
belongs to the guild asking, so a home-server ID can never leak into another guild.
"""

from __future__ import annotations

import logging

import discord

import settings
from firebase_admin import firestore

from data.store import _db

log = logging.getLogger(__name__)


# ── Channel registry ─────────────────────────────────────────────────────────
# key -> (label, description, kind, settings_attr)
#   kind: "text" (text channel) or "category" (channel category)
#   settings_attr: name of the home-guild fallback constant in settings.py (or None)
CHANNEL_TYPES: dict[str, tuple[str, str, str, str | None]] = {
    "weekly_missions":   ("Weekly Missions Board", "Where the weekly missions embed is posted.", "text", "WEEKLY_MISSIONS_CHANNEL_ID"),
    "auction":           ("Auction Listings",      "Where /auction reverse-auction posts go.", "text", "AUCTION_CHANNEL_ID"),
    "checkpoint_photos": ("Checkpoint Photos",     "Where in-game milestone 'hero shots' are posted.", "text", "CHECKPOINT_PHOTOS_CHANNEL_ID"),
    "level_up":          ("Level-Up Announcements","Optional dedicated channel for level-up messages.", "text", "LEVEL_UP_CHANNEL_ID"),
    "contract_mod":      ("Contract Escalations",  "Where contract 'sue' escalations are posted (mod review).", "text", "CONTRACT_MOD_CHANNEL_ID"),
    "ticket_panel":      ("Ticket Panel",          "Channel holding the persistent 'Open a Ticket' button.", "text", "TICKET_PANEL_CHANNEL_ID"),
    "ticket_category":   ("Ticket Category",       "Category under which private ticket channels are created.", "category", "TICKET_CATEGORY_ID"),
    "corp_category":     ("Corp Category",         "Category under which corporation channels are created.", "category", "CORP_CATEGORY_ID"),
}


# ── Role registry ────────────────────────────────────────────────────────────
# Level roles are derived from settings.LEVEL_ROLES (names/descriptions + the
# home-guild fallback ID). The notification, mod, admin and bug_report roles are
# added explicitly. "admin" has deliberately NO settings.py fallback: bot-admin
# authority must be granted per guild on purpose, never inherited from a default.
def _level_role_key(level: int) -> str:
    return f"level_{level}"


def role_label(key: str) -> str:
    """Human label for a role key (used by the /admin setrole UI)."""
    if key == "notifications":
        return "🔔 Notifications (self-assign ping role)"
    if key == "mod":
        return "🛡️ Moderator role"
    if key == "admin":
        return "⭐ Bot-admin role (guild-scoped /admin commands)"
    if key == "bug_report":
        return "🐛 Bug-report role (pinged by in-game bug reports)"
    if key.startswith("level_"):
        try:
            lvl = int(key.split("_", 1)[1])
        except ValueError:
            return key
        info = settings.LEVEL_ROLES.get(lvl)
        if info:
            return f"{info[1]}: {info[2][:60]}"
        return key
    return key


def all_role_keys() -> list[str]:
    keys = [_level_role_key(lvl) for lvl in sorted(settings.LEVEL_ROLES)]
    keys.append("notifications")
    keys.append("mod")
    keys.append("admin")
    keys.append("bug_report")
    return keys


# Keys that must ALL be mapped (to roles that exist in the guild) for the
# achievement-role feature to be enabled in that guild. The mod role is a
# permission concern, not part of the assignable-role gate.
def _required_role_keys() -> list[str]:
    return [_level_role_key(lvl) for lvl in sorted(settings.LEVEL_ROLES)] + ["notifications"]


# ── In-memory cache ──────────────────────────────────────────────────────────
# guild_id (str) -> {"channels": {key: id}, "roles": {key: id}}
_config: dict[str, dict[str, dict[str, int]]] = {}

# Set once `load()` has completed a successful read. `_persist` refuses until then,
# for the reason `store.save()` does: an empty in-memory map is not evidence that
# the stored one is empty, and writing from it can erase a guild's whole config.
# `load()` catches and logs its own failures, so without this a failed boot read
# followed by any single `set_channel` was enough.
_loaded = False


class GuildConfigUnavailable(RuntimeError):
    """Raised when a mapping change cannot be persisted because the boot read failed.

    Deliberately not a startup refusal, unlike `store`'s equivalent: an unloaded wallet
    can lose money, while an unloaded role map only means the mappings are unavailable
    until the next successful load. But it must be LOUD at the point of use, because
    every read also answers None in this state — so `get_admin` 404s every mapped
    bot-admin, achievement roles self-disable and ticket categories become unfindable,
    all while the bot presents as healthy.
    """


def _guild_entry(guild_id: int) -> dict[str, dict[str, int]]:
    gid = str(guild_id)
    if gid not in _config:
        _config[gid] = {"channels": {}, "roles": {}}
    return _config[gid]


# ── Defaults from settings.py ────────────────────────────────────────────────

def _channel_default(key: str) -> int | None:
    meta = CHANNEL_TYPES.get(key)
    if not meta or not meta[3]:
        return None
    return getattr(settings, meta[3], None)


def _role_default(key: str) -> int | None:
    if key == "mod":
        return settings.MOD_ROLE_ID
    if key == "bug_report":
        return settings.BUG_REPORT_ROLE_ID
    if key.startswith("level_"):
        try:
            lvl = int(key.split("_", 1)[1])
        except ValueError:
            return None
        info = settings.LEVEL_ROLES.get(lvl)
        return info[0] if info else None
    return None  # notifications has no home-guild default


# ── Reads ────────────────────────────────────────────────────────────────────

def get_channel_id(guild_id: int, key: str) -> int | None:
    """Configured channel id for this guild, else the settings.py default (raw —
    NOT guild-validated; use resolve_channel before sending)."""
    configured = _guild_entry(guild_id)["channels"].get(key)
    if configured:
        return configured
    return _channel_default(key)


def resolve_channel(bot, guild_id: int, key: str):
    """Return the configured channel object, but only if it exists AND belongs to
    `guild_id`. This is the safe accessor: a settings.py fallback that lives in the
    home guild will resolve to None for any other guild, so content can never be
    posted into the wrong server."""
    cid = get_channel_id(guild_id, key)
    if not cid:
        return None
    ch = bot.get_channel(cid)
    if ch is None:
        return None
    ch_guild_id = getattr(getattr(ch, "guild", None), "id", None)
    if ch_guild_id != guild_id:
        return None
    return ch


def any_channel_configured(bot, key: str) -> bool:
    """True if at least one guild the bot is in has `key` resolvable to a real
    channel. Used to gate globally-mirrored features (marketplace, auctions)."""
    for guild in getattr(bot, "guilds", []) or []:
        if resolve_channel(bot, guild.id, key) is not None:
            return True
    return False


def get_role_id(guild_id: int, key: str) -> int | None:
    """Configured role id for this guild, else the settings.py default (raw)."""
    configured = _guild_entry(guild_id)["roles"].get(key)
    if configured:
        return configured
    return _role_default(key)


def resolve_role(guild: discord.Guild, key: str):
    """Return the configured role object, but only if it exists in `guild`."""
    if guild is None:
        return None
    rid = get_role_id(guild.id, key)
    if not rid:
        return None
    return guild.get_role(rid)


def roles_ready(guild: discord.Guild) -> bool:
    """True only when every required assignable role (all level roles + the
    notification role) is mapped to a role that currently exists in this guild.
    The achievement-role feature self-disables in any guild where this is False."""
    if guild is None:
        return False
    return all(resolve_role(guild, key) is not None for key in _required_role_keys())


def missing_role_keys(guild: discord.Guild) -> list[str]:
    """Required role keys that are not yet mapped to an existing role in `guild`."""
    if guild is None:
        return list(_required_role_keys())
    return [key for key in _required_role_keys() if resolve_role(guild, key) is None]


# ── Writes ───────────────────────────────────────────────────────────────────

def set_channel(guild_id: int, key: str, channel_id: int | None) -> None:
    entry = _guild_entry(guild_id)["channels"]
    if channel_id is None:
        entry.pop(key, None)
        _persist(guild_id, removed=("config_channels", key))
        return
    entry[key] = int(channel_id)
    _persist(guild_id)


def clear_channel(guild_id: int, key: str) -> None:
    set_channel(guild_id, key, None)


def set_role(guild_id: int, key: str, role_id: int | None) -> None:
    entry = _guild_entry(guild_id)["roles"]
    if role_id is None:
        entry.pop(key, None)
        _persist(guild_id, removed=("config_roles", key))
        return
    entry[key] = int(role_id)
    _persist(guild_id)


def clear_role(guild_id: int, key: str) -> None:
    set_role(guild_id, key, None)


# ── Persistence ──────────────────────────────────────────────────────────────

def _persist(guild_id: int, *, removed: tuple[str, str] | None = None) -> None:
    """Write the mappings back.

    `removed` names a (container_field, key) pair that was just deleted, and it is
    NOT optional bookkeeping — without it a clear does not persist at all.

    `set(..., merge=True)` builds its update mask from the leaf field paths present
    in the payload. A key popped from the in-memory map contributes no path, so it
    is not in the mask and Firestore leaves the stored value exactly where it was;
    `load()` then re-adopts it on the next boot. Verified against the installed SDK:

        {"config_channels": {"corp": 1}, "config_roles": {"mod": 9}}
          -> mask ['config_channels.corp', 'config_roles.mod']

    So "Clear this mapping" redrew the embed as unset, made `get_admin` 404, and
    silently handed the role its console access back at the next restart. The same
    held for every key, `mod` and the ticket category included.

    A removal is therefore an explicit `DELETE_FIELD` on that one path. The
    surviving keys still go through the merge, so a concurrent write to the other
    container is not clobbered.

    Note also why the empty-map case must not simply be merged: when a container is
    `{}`, `extract_fields` emits the CONTAINER path, which replaces the whole stored
    map. Combined with a `load()` that only logs its failures, one `set_channel`
    after a failed load would have erased that guild's entire role map — the EC2
    lesson ("a failed load must not authorise a write"), which `store` learned and
    this module had not. `_loaded` below is that guard.
    """
    if not _loaded:
        # RAISE, do not return. Refusing the write is right (see above); doing it
        # silently is not. `set_role`/`set_channel` returned None either way, so the
        # admin panel redrew the embed with the mapping shown as SET and logged that it
        # had mapped it — and it was gone at the next restart. That is precisely the
        # sentence the wallet's own guard was written around: refusing a change is
        # recoverable, silently losing it is not. The callers surface this.
        raise GuildConfigUnavailable(
            "The guild configuration never finished loading, so this change cannot be "
            "saved. Check the bot's Firestore connection and try again after a restart."
        )
    entry = _guild_entry(guild_id)
    try:
        doc = _db.collection("guilds").document(str(guild_id))
        if removed is not None:
            container, key = removed
            doc.update({f"{container}.{key}": firestore.DELETE_FIELD})
            return
        doc.set(
            {"config_channels": entry["channels"], "config_roles": entry["roles"]},
            merge=True,
        )
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to save guild config for %s: %s", guild_id, exc)


def load() -> None:
    """Load all per-guild channel/role config from Firestore. Call at startup,
    next to gkchannels.load_gk_channels()."""
    global _loaded
    try:
        n_ch = n_role = 0
        for doc in _db.collection("guilds").stream():
            data = doc.to_dict() or {}
            channels = {k: int(v) for k, v in (data.get("config_channels") or {}).items() if v}
            roles = {k: int(v) for k, v in (data.get("config_roles") or {}).items() if v}
            if channels or roles:
                _config[doc.id] = {"channels": channels, "roles": roles}
                n_ch += len(channels)
                n_role += len(roles)
        _loaded = True
        log.info("Loaded guild config: %d channel mappings, %d role mappings across %d guilds",
                 n_ch, n_role, len(_config))
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to load guild config: %s", exc)
        # Name the consequences rather than only the cause. In this state every
        # resolve_* answers None, so the bot runs with no mapped admin role, no ticket
        # categories and no achievement roles — and every attempt to set one now raises
        # instead of silently reverting at the next restart.
        log.warning(
            "Guild config is UNLOADED: role and channel mappings will resolve as unset "
            "(no bot-admin console access, no ticket categories, no achievement roles), "
            "and /admin setrole|setchannel will refuse. Restart once Firestore is "
            "reachable to recover."
        )
