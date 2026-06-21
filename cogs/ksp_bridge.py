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
from discord.ui import View, Button, DynamicItem

from api_auth import generate_link_code, resolve_approval
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


# ── KSP login-approval buttons ────────────────────────────────────────────────
#
# DM'd to the user when a KSP client enters their link code. Pressing "Log in"
# approves the waiting client; "Not me" denies it. Both use DynamicItem so they
# keep working across a bot restart; the challenge_id is carried in the custom_id
# (token_urlsafe → no ':' to clash with the separator). resolve_approval verifies
# the clicker actually owns the challenge before applying the decision.

async def _finish_approval(interaction: discord.Interaction, challenge_id: str, approve: bool):
    ok = await asyncio.to_thread(
        resolve_approval, challenge_id, str(interaction.user.id), approve)
    if not ok:
        msg = "⌛ This login request has expired or was already handled."
        color = discord.Color.greyple()
    elif approve:
        msg = "✅ Login approved — switch back to KSP, it should link automatically."
        color = discord.Color.green()
    else:
        msg = "🚫 Login denied. If that wasn't you, your link code is now useless — generate a fresh one only when *you* want to link."
        color = discord.Color.red()
    e = discord.Embed(description=msg, color=color)
    # Replace the prompt so the buttons can't be pressed again.
    await interaction.response.edit_message(embed=e, view=None)


class KSPLoginButton(DynamicItem[Button], template=r"ksp_login:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="✅ Log in", style=discord.ButtonStyle.green,
                                custom_id=f"ksp_login:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_approval(interaction, self.chid, approve=True)


class KSPDenyButton(DynamicItem[Button], template=r"ksp_deny:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="🚫 Not me", style=discord.ButtonStyle.red,
                                custom_id=f"ksp_deny:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_approval(interaction, self.chid, approve=False)


class LinkApprovalView(View):
    """The Log-in / Not-me button pair attached to the approval DM."""
    def __init__(self, challenge_id: str):
        super().__init__(timeout=None)
        self.add_item(KSPLoginButton(challenge_id))
        self.add_item(KSPDenyButton(challenge_id))


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
    # Register the login-approval buttons so DM'd prompts keep working after a
    # bot restart (custom_id carries the challenge_id).
    bot.add_dynamic_items(KSPLoginButton, KSPDenyButton)
