"""
i18n.py – Internationalisation / translation system (two-tier).

Two language layers:
    t(guild_id, key)            → Server language (public messages: level-ups, corp embeds, leaderboards)
    tp(guild_id, user_id, key)  → Personal language (ephemeral/private responses: rank, balance, errors)
"""

import logging
from data.store import _db, store

log = logging.getLogger(__name__)

# In-memory caches
_guild_langs: dict[str, str] = {}   # guild_id -> lang
_user_langs: dict[str, str] = {}    # user_id -> lang (global; the wallet is global)

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en",)
LANG_NAMES = {"en": "English"}

# ── Translation strings ─────────────────────────────────────────────────────
# Format: KEY: {"en": "..."}
# Use {placeholders} for dynamic values.

S: dict[str, dict[str, str]] = {
    # ── General ──────────────────────────────────────────────────────────────
    "general.help.title":           {"en": "📖 Bot Commands"},
    "general.help.desc":            {"en": "Use `{pfx}<command>` for slash commands."},
    "general.help.general":         {"en": "🔵 General"},
    "general.help.info":            {"en": "ℹ️ Info"},
    "general.help.admin":           {"en": "🔴 Admin"},
    "general.help.mod":             {"en": "🟠 Moderation"},
    "general.help.footer":          {"en": "All actions are logged."},
    "general.ping.title":           {"en": "🏓 Pong!"},
    "general.ping.latency":         {"en": "**Latency:** `{ms} ms`"},

    # ── XP ───────────────────────────────────────────────────────────────────
    "xp.level_up.title":            {"en": "🚀 Level Up!"},
    "xp.level_up.desc":             {"en": "**{user}** reached **Level {level}**!\nTotal XP: `{xp}` · Next level: `{next_xp}` XP\n{symbol} **+{reward} {currency}** (Balance: `{balance}`)"},
    "xp.rank.title":                {"en": "📊 {name}'s Rank"},
    "xp.rank.level":                {"en": "Level"},
    "xp.rank.rank":                 {"en": "Rank"},
    "xp.rank.progress":             {"en": "Progress"},
    "xp.rank.messages":             {"en": "Messages"},
    "xp.lb.title":                  {"en": "🏆 XP Leaderboard"},
    "xp.lb.empty":                  {"en": "No data yet, start chatting!"},
    "xp.setxp.done":                {"en": "✅ Set **{name}**'s XP to `{xp}` (Level {level})"},

    # ── Economy ──────────────────────────────────────────────────────────────
    "eco.balance.title":            {"en": "{symbol} {name}'s Balance"},
    "eco.balance.footer":           {"en": "Earn {currency} by leveling up!"},
    "eco.pay.cant_bot":             {"en": "❌ You can't pay a bot."},
    "eco.pay.cant_self":            {"en": "❌ You can't pay yourself."},
    "eco.pay.min":                  {"en": "❌ Minimum transfer is **{min}** {currency}."},
    "eco.pay.insufficient":         {"en": "❌ Insufficient funds. You have **{balance}** {currency}."},
    "eco.pay.title":                {"en": "{symbol} Transfer Complete"},
    "eco.pay.desc":                 {"en": "**{sender}** → **{receiver}**\nAmount: **{amount}** {currency}"},
    "eco.richest.title":            {"en": "{symbol} Richest Members"},
    "eco.richest.empty":            {"en": "Nobody has any KCoins yet!"},
    "eco.give.title":               {"en": "{symbol} Funds Granted"},
    "eco.give.desc":                {"en": "**{name}** received **{amount}** {currency}\n📝 Reason: {reason}\nNew balance: `{balance}`"},
    "eco.fine.title":               {"en": "⚖️ Fine Issued"},
    "eco.fine.desc":                {"en": "**{name}** was fined **{amount}** {currency}\n📝 Reason: {reason}\nNew balance: `{balance}`"},
    "eco.fine.dm":                  {"en": "⚖️ You were fined **{amount} {currency}** in **{guild}**.\n**Reason:** {reason}"},
    "eco.setbal.done":              {"en": "✅ Set **{name}**'s balance to **{amount}** {currency}"},

    # ── Corps ────────────────────────────────────────────────────────────────
    "corps.setup.title":            {"en": "🏢 {name}"},
    "corps.setup.desc":             {"en": "Welcome to your corporation headquarters!"},
    "corps.setup.founder":          {"en": "Founder"},
    "corps.setup.established":      {"en": "Established"},
    "corps.setup.done":             {"en": "🏢 Corporation **{name}** established! Head over to {channel}"},
    "corps.replace.title":          {"en": "⚠️ Corporation Already Exists"},
    "corps.replace.desc":           {"en": "You already own **{old}** in **{guild}**.\n\nReplacing it will **delete** the old channel (<#{channel}>) and create a new one called **{new}**.\n\nDo you want to proceed?"},
    "corps.replace.btn_confirm":    {"en": "Replace Corporation"},
    "corps.replace.btn_cancel":     {"en": "Cancel"},
    "corps.replace.confirming":     {"en": "✅ Replacing your corporation…"},
    "corps.replace.cancelled":      {"en": "❌ Corporation replacement cancelled."},
    "corps.replace.check_dm":       {"en": "📩 You already have a corporation. Check your DMs for replacement options."},
    "corps.replace.no_dm":          {"en": "❌ I can't DM you. Please enable DMs from server members and try again."},
    "corps.replace.done":           {"en": "✅ Corporation **{name}** has been established in **{guild}**! Check out <#{channel}>"},

    # ── Roles ────────────────────────────────────────────────────────────────
    "roles.select_placeholder":     {"en": "Select KSP Titles to equip..."},
    "roles.none_unlocked":          {"en": "None unlocked"},
    "roles.no_titles":              {"en": "No titles unlocked yet."},
    "roles.invalid_selection":      {"en": "❌ You selected a level you haven't unlocked."},
    "roles.updated":                {"en": "✅ Roles updated! Equipped **{count}** title(s)."},
    "roles.cmd_no_unlocked":        {"en": "❌ You have not unlocked any KSP titles yet. Complete missions or upload screenshots to earn them!"},
    "roles.embed_title":            {"en": "🎖️ KSP Title Selector"},
    "roles.embed_desc":             {"en": "Select which KSP achievement titles you want to display on your profile. You can equip multiple titles!"},
    "roles.check_dm":               {"en": "✅ Check your DMs for the title selector!"},
    "roles.no_dm":                  {"en": "❌ I cannot send you a DM. Please enable direct messages from server members."},
    "roles.unlocked_title":         {"en": "🎉 New KSP Achievement Unlocked!"},
    "roles.unlocked_desc":          {"en": "Congratulations! You've achieved **{title_name}** (`{desc}`).\n\nYou can now equip this title in the server using the menu below. You can display multiple titles at once!"},
    "roles.mod_remove_all":         {"en": "Remove ALL Levels"},
    "roles.mod_remove_all_desc":    {"en": "Revoke all level titles from this user"},
    "roles.mod_level_name":         {"en": "Level {lvl}: {name}"},
    "roles.mod_select_placeholder": {"en": "Select level(s) to remove from {name}..."},
    "roles.mod_no_perms":           {"en": "❌ Database updated, but I lack permissions to remove Discord roles."},
    "roles.mod_success":            {"en": "✅ Successfully removed **{count}** level(s) from {user}."},
    "roles.mod_no_unlocked":        {"en": "❌ {user} does not have any KSP level roles unlocked."},
    "roles.mod_embed_title":        {"en": "🛠️ Mod Role Removal"},
    "roles.mod_embed_desc":         {"en": "Select the KSP levels to remove from {user}.\nYou can select specific levels, or choose **Remove ALL Levels**."},

    # ── Common ───────────────────────────────────────────────────────────────
    "common.no_perm":               {"en": "❌ You don't have permission to use this command."},
    "common.error":                 {"en": "💥 An error occurred."},
    "common.amount_positive":       {"en": "❌ Amount must be positive."},
    "common.amount_negative":       {"en": "❌ Balance can't be negative."},
    "common.server_only":           {"en": "❌ This command can only be used in a server."},
    "common.mod_only":              {"en": "🔒 This action is only available in-game. Use the GeneKerman mod inside KSP to do this."},
    "common.issued_by":             {"en": "Issued by {name}"},
}


# ═══════════════════════════════════════════════════════════════════════════
#  SERVER LANGUAGE — public messages (level-ups, corp embeds, leaderboards)
# ═══════════════════════════════════════════════════════════════════════════

def get_server_lang(guild_id: int | None) -> str:
    """Get the server-wide language (for public messages)."""
    if guild_id is None:
        return DEFAULT_LANG
    lang = _guild_langs.get(str(guild_id))
    return lang if lang else DEFAULT_LANG


def set_server_lang(guild_id: int, lang: str) -> None:
    """Set server language (mod-only). Persists to Firestore guild doc."""
    lang = lang.lower()
    if lang not in SUPPORTED_LANGS:
        return
    _guild_langs[str(guild_id)] = lang
    try:
        _db.collection("guilds").document(str(guild_id)).set(
            {"language": lang}, merge=True
        )
    except Exception as exc:
        log.error("Failed to save server language: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
#  PERSONAL LANGUAGE — ephemeral/private responses
# ═══════════════════════════════════════════════════════════════════════════

def get_user_lang(guild_id: int | None, user_id: int | None) -> str:
    """Get user's personal language (global). Falls back to server lang, then default."""
    if user_id is not None:
        lang = _user_langs.get(str(user_id))
        if lang:
            return lang
    return get_server_lang(guild_id)


def set_user_lang(guild_id: int, user_id: int, lang: str) -> None:
    """Set user's personal language (global). Persists to the global user doc."""
    lang = lang.lower()
    if lang not in SUPPORTED_LANGS:
        return
    _user_langs[str(user_id)] = lang
    # Save in the user's data record
    try:
        user = store.get_user(guild_id, user_id)
        user["language"] = lang
        store._mark_dirty(guild_id, user_id)
    except Exception as exc:
        log.error("Failed to save user language: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
#  STARTUP LOADER
# ═══════════════════════════════════════════════════════════════════════════

def load_all_langs() -> None:
    """Load language preferences from Firestore. Call at startup after store.load()."""
    # Load server languages from guild docs
    try:
        for doc in _db.collection("guilds").stream():
            data = doc.to_dict() or {}
            if "language" in data and data["language"]:
                _guild_langs[doc.id] = data["language"]
    except Exception as exc:
        log.error("Failed to load server language prefs: %s", exc)

    # Load user languages from the already-loaded global store
    user_count = 0
    for user_id, data in store._users.items():
        if data.get("language"):
            _user_langs[user_id] = data["language"]
            user_count += 1

    log.info("Loaded language prefs: %d guild(s), %d user(s)", len(_guild_langs), user_count)


# ═══════════════════════════════════════════════════════════════════════════
#  TRANSLATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _translate(lang: str, key: str, **kwargs) -> str:
    """Internal: look up a key in a given language."""
    entry = S.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry.get("en") or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def t(guild_id: int | None, key: str, **kwargs) -> str:
    """Translate using SERVER language (for public messages)."""
    return _translate(get_server_lang(guild_id), key, **kwargs)


def tp(guild_id: int | None, user_id: int | None, key: str, **kwargs) -> str:
    """Translate using PERSONAL language (for ephemeral/private responses)."""
    return _translate(get_user_lang(guild_id, user_id), key, **kwargs)


# Backwards compatibility aliases
get_lang = get_server_lang
set_lang = set_server_lang
