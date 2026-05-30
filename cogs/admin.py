"""
cogs/admin.py – Administrative commands (bot owner / server admins only).
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands
from config import cfg

log = logging.getLogger(__name__)


def is_admin():
    """Check: user must have Administrator permission or be the bot owner."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == cfg.OWNER_ID:
            return True
        if isinstance(interaction.user, discord.Member):
            return interaction.user.guild_permissions.administrator
        return False
    return app_commands.check(predicate)


def is_owner():
    """Check: user must be the bot owner (set via BOT_OWNER_ID in .env)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == cfg.OWNER_ID
    return app_commands.check(predicate)


class Admin(commands.Cog, name="Admin"):
    """Commands restricted to server admins and the bot owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /announce ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="announce",
        description="Send an announcement embed to a channel (Admin only)",
    )
    @app_commands.describe(
        channel="Target channel",
        title="Embed title",
        message="Embed body text",
        color="Hex color code e.g. #5865F2 (optional)",
    )
    @is_admin()
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        message: str,
        color: str = "#5865F2",
    ) -> None:
        try:
            hex_color = int(color.lstrip("#"), 16)
        except ValueError:
            hex_color = 0x5865F2

        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color(hex_color),
        )
        embed.set_footer(text=f"Announced by {interaction.user}")
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Announcement sent to {channel.mention}", ephemeral=True
        )
        log.info("%s sent announcement to #%s", interaction.user, channel.name)

    # ── /reload ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="reload", description="Reload a cog without restarting (Owner only)"
    )
    @app_commands.describe(cog="Cog module path e.g. cogs.general")
    @is_owner()
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        try:
            await self.bot.reload_extension(cog)
            await interaction.response.send_message(
                f"🔄 Reloaded `{cog}`", ephemeral=True
            )
            log.info("%s reloaded %s", interaction.user, cog)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Failed to reload `{cog}`: {exc}", ephemeral=True
            )
            log.error("Reload error for %s: %s", cog, exc)

    # ── /shutdown ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="shutdown", description="Gracefully shut the bot down (Owner only)"
    )
    @is_owner()
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("👋 Shutting down…", ephemeral=True)
        log.warning("%s initiated shutdown", interaction.user)
        await self.bot.close()

    # ── /setprefix ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="setprefix",
        description="Change the bot's prefix command character (Admin only)",
    )
    @app_commands.describe(prefix="New prefix character(s)")
    @is_admin()
    async def setprefix(
        self, interaction: discord.Interaction, prefix: str
    ) -> None:
        self.bot.command_prefix = prefix
        await interaction.response.send_message(
            f"✅ Prefix changed to `{prefix}`", ephemeral=True
        )
        log.info("%s changed prefix to '%s'", interaction.user, prefix)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
        else:
            log.error("Admin cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "💥 An error occurred.", ephemeral=True
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
