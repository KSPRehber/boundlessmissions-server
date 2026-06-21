"""
cogs/ksp_bridge.py – Discord ↔ KSP bridge commands.

Provides:
  /g linkcode — Generate a 6-digit code for KSP account linking
  Persistent "🎮 Link KSP" button in missions channel
"""

import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands

from api_auth import generate_link_code
from i18n import S, tp

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "ksp.linkcode.title":  {"en": "🎮 KSP Link Code"},
    "ksp.linkcode.desc":   {"en": "Enter this code in KSP:\n\n# `{code}`\n\n⏰ Expires in 3 minutes."},
    "ksp.linkcode.footer": {"en": "Gene Kerman KSP Mod"},
    "ksp.linked.title":    {"en": "✅ KSP Linked"},
    "ksp.linked.desc":     {"en": "Your KSP account has been linked successfully!"},
})


class KSPBridge(commands.Cog, name="KSPBridge"):
    """Discord ↔ KSP mod integration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="linkcode", description="Generate a 6-digit code to link your KSP game")
    async def linkcode(self, interaction: discord.Interaction):
        """Generate a link code for KSP account linking."""
        # Acknowledge immediately: generate_link_code makes blocking Firestore
        # calls (query + deletes + write) that can exceed Discord's 3-second
        # interaction window and otherwise raise 10062 (Unknown interaction).
        await interaction.response.defer(ephemeral=True)

        gid = interaction.guild_id
        uid = interaction.user.id
        username = interaction.user.display_name

        # Run the blocking Firestore work off the event loop.
        code = await asyncio.to_thread(generate_link_code, gid, uid, username)

        embed = discord.Embed(
            title=tp(gid, uid, "ksp.linkcode.title"),
            description=tp(gid, uid, "ksp.linkcode.desc", code=code),
            color=discord.Color.from_rgb(0, 180, 100),
        )
        embed.set_footer(text=tp(gid, uid, "ksp.linkcode.footer"))
        embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1510200111253291258.webp")

        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s generated KSP link code", interaction.user)


async def setup(bot: commands.Bot):
    await bot.add_cog(KSPBridge(bot))
