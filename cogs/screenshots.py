"""
cogs/screenshots.py – KSP Screenshot Analysis via Gemini AI.

Two modes:
  1. /gk analyze            → auto-finds your most recent image message above
  2. /gk analyze image:...  → directly analyzes uploaded image(s)

Multiple images are processed individually. Already-reviewed messages are skipped.
Only the original poster can analyze via auto-detect mode.
"""

import json
import logging
import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from google import genai
from google.genai import types

from i18n import t, tp, S
import settings
from data.store import store

log = logging.getLogger(__name__)

# ── Gemini setup ─────────────────────────────────────────────────────────────

_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

if _GEMINI_KEY and _GEMINI_KEY != "YOUR_GEMINI_API_KEY_HERE":
    _client = genai.Client(api_key=_GEMINI_KEY)
    _MODEL = "gemini-3.1-flash-lite"
    log.info("Gemini AI configured (%s)", _MODEL)
else:
    _client = None
    _MODEL = None
    log.warning("GEMINI_API_KEY not set — screenshot analysis disabled")


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Kerbal Space Program (KSP) and KSP2 screenshot analyst. You are an encyclopedia of every celestial body in KSP modding and stock.

Your job: analyze a screenshot and return ONLY a valid JSON object. No markdown, no commentary, just raw JSON.

## Celestial Bodies Knowledge Base

### Stock Kerbol System
- **Kerbol** (Sun/Star)
- **Moho** — innermost planet, no atmosphere, high gravity relative to size
- **Eve** — purple atmosphere, thick, ocean of Explodium, very hard to return from. Moon: **Gilly** (tiny asteroid)
- **Kerbin** — home planet, blue-green, KSC visible. Moons: **Mun** (grey, craters), **Minmus** (mint green, flats)
- **Duna** — red/rust, thin atmosphere. Moon: **Ike** (grey, tidally locked)
- **Dres** — asteroid belt dwarf planet, grey, canyon
- **Jool** — green gas giant, banded atmosphere. Moons: **Laythe** (ocean world, blue, atmosphere), **Vall** (icy, smooth), **Tylo** (large, no atmosphere, high gravity), **Bop** (tiny, dark, irregular), **Pol** (tiny, yellowish)
- **Eeloo** — distant dwarf planet, icy white/blue

### Outer Planets Mod (OPM)
- **Sarnus** — ringed yellow gas giant. Moons: **Hale**, **Ovok**, **Eeloo** (moved here), **Slate**, **Tekto** (atmosphere, orange)
- **Urlum** — blue-green ice giant, ringed. Moons: **Polta**, **Priax**, **Wal** (+ sub-moon **Tal**)
- **Neidon** — blue ice giant. Moons: **Thatmo** (atmosphere, retrograde), **Nissee**
- **Plock** — distant dwarf. Moon: **Karen**

### Kcalbeloh System (Interstellar mod)
- **Kcalbeloh** — black hole at center, accretion disk
- Orbiting bodies include: **Suluco**, **Yeldo**, **Noyreg**, **Efil**, **Otsol**, **Ambrosh**, many more
- Distinctive visual: black void with bright accretion ring, extreme gravitational lensing

### Real Solar System (RSS)
- Replaces Kerbol system with real solar system: Sun, Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus, Neptune, Pluto, plus all real moons
- Earth replaces Kerbin, Moon replaces Mun, etc.

### Other Common Mods
- **Galaxies Unbound** — multiple star systems
- **Beyond Home** — custom star system replacing stock
- **Parallax** — enhanced terrain (visible in surface shots)
- **EVE/Scatterer** — volumetric clouds, atmospheric scattering
- **Restock** — updated part textures

## Difficulty Rating Scale (1-10)
Rate how difficult the DEPICTED mission/achievement would be to accomplish AND RETURN safely:

1. **1** — Launch pad / runway scene, pre-launch
2. **2** — Suborbital flight, basic atmosphere flight
3. **3** — Kerbin orbit achieved
4. **4** — Mun/Minmus flyby or orbit
5. **5** — Mun/Minmus landing and return, interplanetary flyby
6. **6** — Duna/Eve orbit, inner system landings (Moho, Gilly)
7. **7** — Duna landing + return, Jool system operations, Eve orbit + return
8. **8** — Tylo landing, Eve ascent vehicle, large station construction, Jool-5
9. **9** — Grand tour, interstellar travel (Kcalbeloh), full colonization
10. **10** — Completing seemingly impossible feats: Eve SSTO return, full interstellar colonization, grand tour SSTO

## Crew Detection
- Look for crew portraits in bottom-right corner
- Look for IVA (interior) views showing kerbals
- EVA (spacewalk) scenes show a kerbal outside
- Probe cores = uncrewed

## Required JSON Schema
```json
{
  "approved": true,
  "location": {
    "celestial_body": "Name of planet/moon/star",
    "system": "stock/opm/kcalbeloh/rss/beyond_home/unknown",
    "situation": "prelaunch/launched/flying/suborbital/orbiting/suborbit_reentry/landed/splashed/escaping/docked",
    "biome": "Name if identifiable, else null",
    "altitude_estimate": "low orbit / high orbit / surface / atmosphere / deep space"
  },
  "craft": {
    "crewed": true,
    "crew_count_estimate": 1,
    "craft_type": "rocket/spaceplane/lander/rover/station/satellite/probe/ssto/shuttle/flag/eva/unknown",
    "notable_features": ["description of visible craft elements"]
  },
  "visual_mods": ["list of visual mods detected: EVE, Scatterer, Parallax, Restock, Waterfall, etc."],
  "difficulty_rating": 5,
  "difficulty_reason": "Brief explanation of why this rating",
  "description": "2-3 sentence description of what the screenshot shows",
  "mission_phase": "ascent/transfer/orbit_insertion/landing/surface_ops/return/docking/eva/construction/reentry/recovery"
}
```

## Rules
- If the image is NOT from KSP/KSP2, set `"approved": false` and set `difficulty_rating` to 0 and all other fields to null/empty
- ALWAYS return valid JSON only. No markdown fences, no explanation text
- Be specific about celestial bodies — don't guess randomly
- If unsure about a body, use the visual cues (color, terrain, atmosphere, rings)
- Rate based on the FULL mission difficulty including return, not just getting there
"""


# ── i18n strings ─────────────────────────────────────────────────────────────

S.update({
    "ss.analyzing":       {"tr": "🔍 Ekran görüntüsü analiz ediliyor…",
                           "en": "🔍 Analyzing screenshot…"},
    "ss.no_image":        {"tr": "❌ Görüntü bulunamadı. Komutu bir ekran görüntüsünün altında kullanın veya bir görüntü ekleyin.",
                           "en": "❌ No image found. Use this command below a screenshot or attach an image."},
    "ss.not_approved":    {"tr": "❌ Bu bir KSP ekran görüntüsü değil.",
                           "en": "❌ This is not a KSP screenshot."},
    "ss.error":           {"tr": "💥 Analiz sırasında bir hata oluştu.",
                           "en": "💥 An error occurred during analysis."},
    "ss.no_api_key":      {"tr": "❌ Gemini API anahtarı ayarlanmamış.",
                           "en": "❌ Gemini API key not configured."},
    "ss.already_reviewed":{"tr": "❌ Bu ekran görüntüsü zaten analiz edildi.",
                           "en": "❌ This screenshot has already been analyzed."},
    "ss.not_yours":       {"tr": "❌ Yalnızca kendi gönderdiğiniz ekran görüntülerini analiz edebilirsiniz.",
                           "en": "❌ You can only analyze screenshots that you posted."},
    "ss.title":           {"tr": "🔭 KSP Ekran Görüntüsü Analizi",
                           "en": "🔭 KSP Screenshot Analysis"},
    "ss.location":        {"tr": "📍 Konum", "en": "📍 Location"},
    "ss.situation":       {"tr": "🛰️ Durum", "en": "🛰️ Situation"},
    "ss.craft":           {"tr": "🚀 Araç", "en": "🚀 Craft"},
    "ss.difficulty":      {"tr": "⭐ Zorluk", "en": "⭐ Difficulty"},
    "ss.mods":            {"tr": "🎨 Görsel Modlar", "en": "🎨 Visual Mods"},
    "ss.phase":           {"tr": "📋 Görev Aşaması", "en": "📋 Mission Phase"},
    "ss.crewed":          {"tr": "Mürettebatlı", "en": "Crewed"},
    "ss.uncrewed":        {"tr": "Mürettebatsız", "en": "Uncrewed"},
    "ss.analyzed_by":     {"tr": "Analiz: {name} tarafından",
                           "en": "Analyzed by {name}"},
    "ss.img_counter":     {"tr": "📸 Görüntü {n}/{total}",
                           "en": "📸 Image {n}/{total}"},
    "ss.reward":          {"tr": "🎁 Ödül", "en": "🎁 Reward"},
})


# ── Reviewed message tracking ───────────────────────────────────────────────
_reviewed_messages: set[int] = set()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _difficulty_bar(rating: int) -> str:
    filled = "🟩" if rating <= 3 else "🟨" if rating <= 6 else "🟧" if rating <= 8 else "🟥"
    return filled * rating + "⬛" * (10 - rating)


SITUATION_TR = {
    "prelaunch": "Fırlatma Öncesi", "launched": "Fırlatıldı", "flying": "Uçuş",
    "suborbital": "Altı Yörünge", "orbiting": "Yörüngede", "suborbit_reentry": "Yeniden Giriş",
    "landed": "İniş Yapmış", "splashed": "Suya İnmiş", "escaping": "Kaçış Yörüngesi",
    "docked": "Kenetlenmiş",
}

PHASE_TR = {
    "ascent": "Yükseliş", "transfer": "Transfer", "orbit_insertion": "Yörünge Girişi",
    "landing": "İniş", "surface_ops": "Yüzey Operasyonları", "return": "Dönüş",
    "docking": "Kenetlenme", "eva": "EVA", "construction": "İnşaat",
    "reentry": "Atmosfere Yeniden Giriş", "recovery": "Kurtarma",
}

CRAFT_TYPE_TR = {
    "rocket": "Roket", "spaceplane": "Uzay Uçağı", "lander": "İniş Aracı",
    "rover": "Gezici", "station": "İstasyon", "satellite": "Uydu",
    "probe": "Sonda", "ssto": "SSTO", "shuttle": "Mekik",
    "flag": "Bayrak", "eva": "EVA", "unknown": "Bilinmiyor",
}


def _build_analysis_embed(
    data: dict, guild_id: int, user_name: str, image_url: str | None = None,
) -> discord.Embed:
    """Build a rich embed from the Gemini analysis JSON using server language."""
    loc = data.get("location", {}) or {}
    craft = data.get("craft", {}) or {}
    rating = data.get("difficulty_rating", 0)

    embed = discord.Embed(
        title=t(guild_id, "ss.title"),
        description=data.get("description", ""),
        color=discord.Color.from_rgb(40, 120, 200) if rating < 7 else discord.Color.from_rgb(200, 80, 40),
    )

    body = loc.get("celestial_body", "?")
    system = loc.get("system", "unknown")
    altitude = loc.get("altitude_estimate", "")
    biome = loc.get("biome") or ""
    loc_text = f"**{body}**"
    if system and system != "unknown":
        loc_text += f" ({system})"
    if biome:
        loc_text += f"\n🏔️ {biome}"
    if altitude:
        loc_text += f"\n📏 {altitude}"
    embed.add_field(name=t(guild_id, "ss.location"), value=loc_text, inline=True)

    situation_raw = loc.get("situation", "unknown")
    from i18n import get_server_lang
    lang = get_server_lang(guild_id)
    situation = SITUATION_TR.get(situation_raw, situation_raw) if lang == "tr" else situation_raw
    embed.add_field(name=t(guild_id, "ss.situation"), value=f"**{situation}**", inline=True)

    is_crewed = craft.get("crewed", False)
    crew_label = t(guild_id, "ss.crewed") if is_crewed else t(guild_id, "ss.uncrewed")
    crew_text = f"{'👨‍🚀' if is_crewed else '🤖'} {crew_label}"
    if craft.get("crew_count_estimate"):
        crew_text += f" (×{craft['crew_count_estimate']})"
    craft_type_raw = craft.get("craft_type", "unknown")
    craft_type = CRAFT_TYPE_TR.get(craft_type_raw, craft_type_raw) if lang == "tr" else craft_type_raw
    features = craft.get("notable_features", [])
    craft_text = f"{crew_text}\n🔧 **{craft_type}**"
    if features:
        craft_text += "\n" + ", ".join(features[:3])
    embed.add_field(name=t(guild_id, "ss.craft"), value=craft_text, inline=True)

    bar = _difficulty_bar(rating)
    reason = data.get("difficulty_reason", "")
    embed.add_field(
        name=t(guild_id, "ss.difficulty"),
        value=f"{bar} **{rating}/10**\n{reason}",
        inline=False,
    )

    mods = data.get("visual_mods", [])
    if mods:
        embed.add_field(name=t(guild_id, "ss.mods"), value=", ".join(mods), inline=True)

    phase_raw = data.get("mission_phase", "")
    if phase_raw:
        phase = PHASE_TR.get(phase_raw, phase_raw) if lang == "tr" else phase_raw
        embed.add_field(name=t(guild_id, "ss.phase"), value=f"**{phase}**", inline=True)

    # Rewards
    xp_reward = rating * settings.SCREENSHOT_XP_PER_DIFFICULTY
    coin_reward = rating * settings.SCREENSHOT_COINS_PER_DIFFICULTY
    currency = settings.CURRENCY_SYMBOL
    embed.add_field(
        name=t(guild_id, "ss.reward"),
        value=f"**+{xp_reward} XP**  ·  **+{coin_reward}** {currency}",
        inline=False,
    )

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=t(guild_id, "ss.analyzed_by", name=user_name))
    return embed


async def _extract_all_images(msg: discord.Message) -> list[tuple[str, bytes]]:
    """Extract ALL images from a message."""
    images = []
    for att in msg.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            data = await att.read()
            images.append((att.url, data))

    if not images:
        for emb in msg.embeds:
            url = None
            if emb.image and emb.image.url:
                url = emb.image.url
            elif emb.thumbnail and emb.thumbnail.url:
                url = emb.thumbnail.url
            if url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            images.append((url, await resp.read()))
    return images


async def _run_gemini(image_list: list[bytes], guild_id: int | None = None) -> dict:
    """Send one or more images to Gemini in a single call and parse the JSON response."""
    from i18n import get_server_lang
    lang = get_server_lang(guild_id)
    if lang == "tr":
        lang_instruction = "ALL text fields (description, difficulty_reason, notable_features) MUST be written in TURKISH (Türkçe). Only celestial body names and technical terms stay in English."
    else:
        lang_instruction = "ALL text fields (description, difficulty_reason, notable_features) must be written in English."

    multi_note = ""
    if len(image_list) > 1:
        multi_note = (
            "\n\n## IMPORTANT: Multiple Images\n"
            f"You are receiving {len(image_list)} screenshots from the SAME mission. "
            "Analyze them together as a single mission and return ONE JSON object. "
            "Base the difficulty_rating on the overall mission difficulty."
        )

    prompt = SYSTEM_PROMPT + multi_note + "\n\n## CRITICAL: Response Language\n" + lang_instruction

    parts = [types.Part.from_text(text=prompt)]
    for img_bytes in image_list:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    response = _client.models.generate_content(
        model=_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
        ),
    )

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()

    return json.loads(raw_text)


async def _grant_rewards(gid: int, uid: int, rating: int) -> tuple[int, int]:
    """Grant XP + KCoins based on difficulty. Returns (xp_awarded, coins_awarded)."""
    xp_reward = rating * settings.SCREENSHOT_XP_PER_DIFFICULTY
    coin_reward = rating * settings.SCREENSHOT_COINS_PER_DIFFICULTY
    if xp_reward > 0:
        await store.set_xp(gid, uid, store.get_user(gid, uid)["xp"] + xp_reward)
    if coin_reward > 0:
        await store.add_balance(gid, uid, coin_reward)
    return xp_reward, coin_reward


# ── Cog ──────────────────────────────────────────────────────────────────────

class Screenshots(commands.Cog, name="Screenshots"):
    """KSP screenshot analysis via Gemini AI."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="analyze",
        description="Analyze a KSP screenshot (attach images or auto-detect above)",
    )
    @app_commands.describe(
        image1="Screenshot to analyze",
        image2="Second screenshot (optional)",
        image3="Third screenshot (optional)",
    )
    async def analyze(
        self,
        interaction: discord.Interaction,
        image1: discord.Attachment | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id

        if _client is None:
            await interaction.response.send_message(
                tp(gid, uid, "ss.no_api_key"), ephemeral=True
            )
            return

        # Defer IMMEDIATELY — prevents interaction timeout
        await interaction.response.defer()

        # ── Mode 1: Direct image(s) uploaded with the command ────────────────
        direct = [a for a in (image1, image2, image3)
                  if a and a.content_type and a.content_type.startswith("image/")]

        if direct:
            all_bytes = [await a.read() for a in direct]
            first_url = direct[0].url

            try:
                data = await _run_gemini(all_bytes, gid)
            except Exception as exc:
                log.error("Gemini error on direct upload: %s", exc, exc_info=True)
                await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
                return

            if not data.get("approved", False):
                embed = discord.Embed(
                    title=t(gid, "ss.not_approved"),
                    description=data.get("description", ""),
                    color=discord.Color.red(),
                )
                embed.set_thumbnail(url=first_url)
                await interaction.followup.send(embed=embed)
                return

            embed = _build_analysis_embed(data, gid, interaction.user.display_name, first_url)
            rating = data.get("difficulty_rating", 0)
            xp_r, coin_r = await _grant_rewards(gid, uid, rating)

            await interaction.followup.send(embed=embed)
            log.info("%s analyzed %d direct upload(s) — difficulty %d (+%d XP, +%d coins)",
                     interaction.user, len(direct), rating, xp_r, coin_r)
            return

        # ── Mode 2: Auto-detect the most recent image message above ──────────
        target_msg = None
        async for msg in interaction.channel.history(limit=10, before=interaction.created_at):
            if msg.author.bot:
                continue
            has_image = any(
                a.content_type and a.content_type.startswith("image/")
                for a in msg.attachments
            )
            if not has_image:
                has_image = any(
                    (e.image and e.image.url) or (e.thumbnail and e.thumbnail.url)
                    for e in msg.embeds
                )
            if has_image:
                target_msg = msg
                break

        if target_msg is None:
            await interaction.followup.send(tp(gid, uid, "ss.no_image"), ephemeral=True)
            return

        if target_msg.author.id != uid:
            await interaction.followup.send(tp(gid, uid, "ss.not_yours"), ephemeral=True)
            return

        if target_msg.id in _reviewed_messages:
            await interaction.followup.send(tp(gid, uid, "ss.already_reviewed"), ephemeral=True)
            return

        images = await _extract_all_images(target_msg)
        if not images:
            await interaction.followup.send(tp(gid, uid, "ss.no_image"), ephemeral=True)
            return

        _reviewed_messages.add(target_msg.id)

        # Send ALL images in one Gemini call
        all_bytes = [img_bytes for _, img_bytes in images]
        first_url = images[0][0]

        try:
            data = await _run_gemini(all_bytes, gid)
        except json.JSONDecodeError as exc:
            log.error("Gemini returned invalid JSON: %s", exc)
            await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
            return
        except Exception as exc:
            log.error("Screenshot analysis error: %s", exc, exc_info=True)
            await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
            return

        if not data.get("approved", False):
            embed = discord.Embed(
                title=t(gid, "ss.not_approved"),
                description=data.get("description", ""),
                color=discord.Color.red(),
            )
            if first_url:
                embed.set_thumbnail(url=first_url)
            await target_msg.reply(embed=embed, mention_author=False)
            await interaction.followup.send("✅", ephemeral=True)
            return

        embed = _build_analysis_embed(data, gid, interaction.user.display_name, first_url)
        rating = data.get("difficulty_rating", 0)
        xp_r, coin_r = await _grant_rewards(gid, uid, rating)

        await target_msg.reply(embed=embed, mention_author=False)
        await interaction.followup.send("✅", ephemeral=True)

        log.info(
            "%s analyzed screenshot (msg %d, %d imgs): %s @ %s — difficulty %d (+%d XP, +%d coins)",
            interaction.user, target_msg.id, len(images),
            data.get("location", {}).get("celestial_body", "?"),
            data.get("location", {}).get("situation", "?"),
            rating, xp_r, coin_r,
        )

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error("Screenshots cog error: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                tp(interaction.guild_id, interaction.user.id, "common.error"),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Screenshots(bot))
