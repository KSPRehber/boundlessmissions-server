"""
i18n.py – Internationalisation / translation system (two-tier).

Two language layers:
    t(guild_id, key)            → Server language (public messages: level-ups, corp embeds, leaderboards)
    tp(guild_id, user_id, key)  → Personal language (ephemeral/private responses: rank, balance, errors)

Server language:  Stored in Firestore guild doc. Changed by mods via /gk tsl.
Personal language: Stored in Firestore user doc. Changed by anyone via /gk langswitch.

Default for both: Turkish (tr).
"""

import logging
from data.store import _db, store

log = logging.getLogger(__name__)

# In-memory caches
_guild_langs: dict[str, str] = {}            # guild_id -> lang
_user_langs: dict[tuple[str, str], str] = {} # (guild_id, user_id) -> lang

DEFAULT_LANG = "tr"
SUPPORTED_LANGS = ("tr", "en")
LANG_NAMES = {"tr": "Türkçe", "en": "English"}

# ── Translation strings ─────────────────────────────────────────────────────
# Format: KEY: {"tr": "...", "en": "..."}
# Use {placeholders} for dynamic values.

S: dict[str, dict[str, str]] = {
    # ── General ──────────────────────────────────────────────────────────────
    "general.help.title":           {"tr": "📖 Bot Komutları", "en": "📖 Bot Commands"},
    "general.help.desc":            {"tr": "`{pfx}<komut>` kullanarak slash komutlarını çalıştırın.", "en": "Use `{pfx}<command>` for slash commands."},
    "general.help.general":         {"tr": "🔵 Genel", "en": "🔵 General"},
    "general.help.info":            {"tr": "ℹ️ Bilgi", "en": "ℹ️ Info"},
    "general.help.admin":           {"tr": "🔴 Yönetici", "en": "🔴 Admin"},
    "general.help.mod":             {"tr": "🟠 Moderasyon", "en": "🟠 Moderation"},
    "general.help.footer":          {"tr": "Tüm işlemler kayıt altındadır.", "en": "All actions are logged."},
    "general.ping.title":           {"tr": "🏓 Pong!", "en": "🏓 Pong!"},
    "general.ping.latency":         {"tr": "**Gecikme:** `{ms} ms`", "en": "**Latency:** `{ms} ms`"},

    # ── XP ───────────────────────────────────────────────────────────────────
    "xp.level_up.title":            {"tr": "🚀 Seviye Atladı!", "en": "🚀 Level Up!"},
    "xp.level_up.desc":             {"tr": "**{user}** **Seviye {level}**'e ulaştı!\nToplam XP: `{xp}` · Sonraki seviye: `{next_xp}` XP\n{symbol} **+{reward} {currency}** (Bakiye: `{balance}`)", "en": "**{user}** reached **Level {level}**!\nTotal XP: `{xp}` · Next level: `{next_xp}` XP\n{symbol} **+{reward} {currency}** (Balance: `{balance}`)"},
    "xp.rank.title":                {"tr": "📊 {name} — Sıralama", "en": "📊 {name}'s Rank"},
    "xp.rank.level":                {"tr": "Seviye", "en": "Level"},
    "xp.rank.rank":                 {"tr": "Sıra", "en": "Rank"},
    "xp.rank.progress":             {"tr": "İlerleme", "en": "Progress"},
    "xp.rank.messages":             {"tr": "Mesajlar", "en": "Messages"},
    "xp.lb.title":                  {"tr": "🏆 XP Sıralaması", "en": "🏆 XP Leaderboard"},
    "xp.lb.empty":                  {"tr": "Henüz veri yok — sohbet etmeye başlayın!", "en": "No data yet — start chatting!"},
    "xp.setxp.done":                {"tr": "✅ **{name}** XP'si `{xp}` olarak ayarlandı (Seviye {level})", "en": "✅ Set **{name}**'s XP to `{xp}` (Level {level})"},

    # ── Economy ──────────────────────────────────────────────────────────────
    "eco.balance.title":            {"tr": "{symbol} {name} — Bakiye", "en": "{symbol} {name}'s Balance"},
    "eco.balance.footer":           {"tr": "Seviye atlayarak {currency} kazanın!", "en": "Earn {currency} by leveling up!"},
    "eco.pay.cant_bot":             {"tr": "❌ Bir bota ödeme yapamazsınız.", "en": "❌ You can't pay a bot."},
    "eco.pay.cant_self":            {"tr": "❌ Kendinize ödeme yapamazsınız.", "en": "❌ You can't pay yourself."},
    "eco.pay.min":                  {"tr": "❌ Minimum transfer: **{min}** {currency}.", "en": "❌ Minimum transfer is **{min}** {currency}."},
    "eco.pay.insufficient":         {"tr": "❌ Yetersiz bakiye. Bakiyeniz: **{balance}** {currency}.", "en": "❌ Insufficient funds. You have **{balance}** {currency}."},
    "eco.pay.title":                {"tr": "{symbol} Transfer Tamamlandı", "en": "{symbol} Transfer Complete"},
    "eco.pay.desc":                 {"tr": "**{sender}** → **{receiver}**\nMiktar: **{amount}** {currency}", "en": "**{sender}** → **{receiver}**\nAmount: **{amount}** {currency}"},
    "eco.richest.title":            {"tr": "{symbol} En Zenginler", "en": "{symbol} Richest Members"},
    "eco.richest.empty":            {"tr": "Henüz kimsenin KCoin'i yok!", "en": "Nobody has any KCoins yet!"},
    "eco.give.title":               {"tr": "{symbol} Para Verildi", "en": "{symbol} Funds Granted"},
    "eco.give.desc":                {"tr": "**{name}** **{amount}** {currency} aldı\n📝 Sebep: {reason}\nYeni bakiye: `{balance}`", "en": "**{name}** received **{amount}** {currency}\n📝 Reason: {reason}\nNew balance: `{balance}`"},
    "eco.fine.title":               {"tr": "⚖️ Ceza Kesildi", "en": "⚖️ Fine Issued"},
    "eco.fine.desc":                {"tr": "**{name}** **{amount}** {currency} ceza aldı\n📝 Sebep: {reason}\nYeni bakiye: `{balance}`", "en": "**{name}** was fined **{amount}** {currency}\n📝 Reason: {reason}\nNew balance: `{balance}`"},
    "eco.fine.dm":                  {"tr": "⚖️ **{guild}** sunucusunda **{amount} {currency}** ceza aldınız.\n**Sebep:** {reason}", "en": "⚖️ You were fined **{amount} {currency}** in **{guild}**.\n**Reason:** {reason}"},
    "eco.setbal.done":              {"tr": "✅ **{name}** bakiyesi **{amount}** {currency} olarak ayarlandı", "en": "✅ Set **{name}**'s balance to **{amount}** {currency}"},

    # ── Corps ────────────────────────────────────────────────────────────────
    "corps.setup.title":            {"tr": "🏢 {name}", "en": "🏢 {name}"},
    "corps.setup.desc":             {"tr": "Şirket merkezinize hoş geldiniz!", "en": "Welcome to your corporation headquarters!"},
    "corps.setup.founder":          {"tr": "Kurucu", "en": "Founder"},
    "corps.setup.established":      {"tr": "Kuruluş Tarihi", "en": "Established"},
    "corps.setup.done":             {"tr": "🏢 **{name}** şirketi kuruldu! {channel} kanalına gidin", "en": "🏢 Corporation **{name}** established! Head over to {channel}"},
    "corps.replace.title":          {"tr": "⚠️ Şirket Zaten Mevcut", "en": "⚠️ Corporation Already Exists"},
    "corps.replace.desc":           {"tr": "**{guild}** sunucusunda zaten **{old}** şirketine sahipsiniz.\n\nDeğiştirmek eski kanalı (<#{channel}>) **silecek** ve **{new}** adında yeni bir tane oluşturacak.\n\nDevam etmek istiyor musunuz?", "en": "You already own **{old}** in **{guild}**.\n\nReplacing it will **delete** the old channel (<#{channel}>) and create a new one called **{new}**.\n\nDo you want to proceed?"},
    "corps.replace.btn_confirm":    {"tr": "Şirketi Değiştir", "en": "Replace Corporation"},
    "corps.replace.btn_cancel":     {"tr": "İptal", "en": "Cancel"},
    "corps.replace.confirming":     {"tr": "✅ Şirketiniz değiştiriliyor…", "en": "✅ Replacing your corporation…"},
    "corps.replace.cancelled":      {"tr": "❌ Şirket değişikliği iptal edildi.", "en": "❌ Corporation replacement cancelled."},
    "corps.replace.check_dm":       {"tr": "📩 Zaten bir şirketiniz var. Değiştirme seçenekleri için DM'lerinizi kontrol edin.", "en": "📩 You already have a corporation. Check your DMs for replacement options."},
    "corps.replace.no_dm":          {"tr": "❌ Size DM atamıyorum. Lütfen sunucu üyelerinden DM'leri etkinleştirin ve tekrar deneyin.", "en": "❌ I can't DM you. Please enable DMs from server members and try again."},
    "corps.replace.done":           {"tr": "✅ **{name}** şirketi **{guild}** sunucusunda kuruldu! <#{channel}> kanalına göz atın", "en": "✅ Corporation **{name}** has been established in **{guild}**! Check out <#{channel}>"},

    # ── Language ─────────────────────────────────────────────────────────────
    "lang.personal.switched":       {"tr": "🌐 Kişisel diliniz **{lang_name}** olarak ayarlandı.", "en": "🌐 Your personal language set to **{lang_name}**."},
    "lang.server.switched":         {"tr": "🌐 Sunucu dili **{lang_name}** olarak ayarlandı.", "en": "🌐 Server language set to **{lang_name}**."},

    # ── Common ───────────────────────────────────────────────────────────────
    "common.no_perm":               {"tr": "❌ Bu komutu kullanma yetkiniz yok.", "en": "❌ You don't have permission to use this command."},
    "common.error":                 {"tr": "💥 Bir hata oluştu.", "en": "💥 An error occurred."},
    "common.amount_positive":       {"tr": "❌ Miktar pozitif olmalıdır.", "en": "❌ Amount must be positive."},
    "common.amount_negative":       {"tr": "❌ Bakiye negatif olamaz.", "en": "❌ Balance can't be negative."},
    "common.server_only":           {"tr": "❌ Bu komut sadece sunucularda kullanılabilir.", "en": "❌ This command can only be used in a server."},
    "common.issued_by":             {"tr": "Yetkili: {name}", "en": "Issued by {name}"},
}


# ═══════════════════════════════════════════════════════════════════════════
#  SERVER LANGUAGE — public messages (level-ups, corp embeds, leaderboards)
# ═══════════════════════════════════════════════════════════════════════════

def get_server_lang(guild_id: int | None) -> str:
    """Get the server-wide language (for public messages)."""
    if guild_id is None:
        return DEFAULT_LANG
    return _guild_langs.get(str(guild_id), DEFAULT_LANG)


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
    """Get user's personal language. Falls back to server lang, then default."""
    if guild_id is not None and user_id is not None:
        key = (str(guild_id), str(user_id))
        if key in _user_langs:
            return _user_langs[key]
    return get_server_lang(guild_id)


def set_user_lang(guild_id: int, user_id: int, lang: str) -> None:
    """Set user's personal language. Persists to Firestore user doc."""
    lang = lang.lower()
    if lang not in SUPPORTED_LANGS:
        return
    _user_langs[(str(guild_id), str(user_id))] = lang
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
            if "language" in data:
                _guild_langs[doc.id] = data["language"]
    except Exception as exc:
        log.error("Failed to load server language prefs: %s", exc)

    # Load user languages from already-loaded store data
    user_count = 0
    for guild_id, users in store._data.items():
        for user_id, data in users.items():
            if "language" in data:
                _user_langs[(guild_id, user_id)] = data["language"]
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
