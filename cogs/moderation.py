"""
cogs/moderation.py – Moderation commands (kick, ban, mute, purge, warn).
Requires Manage Members / Manage Messages / Moderate Members permissions.
"""

import logging
import datetime
import discord
from discord import app_commands
from discord.ext import commands

from cogs import perms

log = logging.getLogger(__name__)


def mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_mod_user(interaction)   # mimic-safe, per-guild mod role
    return app_commands.check(predicate)


class Moderation(commands.Cog, name="Moderation"):
    """Server moderation tools."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._warnings: dict[int, dict[int, list[str]]] = {}

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason for kick")
    @mod_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 **{member}** has been kicked.\n📝 Reason: {reason}")
        log.info("%s kicked %s — %s", interaction.user, member, reason)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="Member to ban", reason="Reason for ban", delete_days="Days of messages to delete (0–7)")
    @mod_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        await member.ban(reason=reason, delete_message_days=delete_days)
        await interaction.response.send_message(f"🔨 **{member}** has been banned.\n📝 Reason: {reason}")
        log.info("%s banned %s — %s", interaction.user, member, reason)

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason")
    @mod_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided") -> None:
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)
            await interaction.response.send_message(f"✅ **{user}** has been unbanned.")
            log.info("%s unbanned %s — %s", interaction.user, user, reason)
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found or not banned.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)

    @app_commands.command(name="mute", description="Timeout (mute) a member")
    @app_commands.describe(member="Member to mute", minutes="Duration in minutes (1–40320)", reason="Reason")
    @mod_only()
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320] = 10, reason: str = "No reason provided") -> None:
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(f"🔇 **{member}** muted for **{minutes} min**.\n📝 Reason: {reason}")
        log.info("%s muted %s for %d min — %s", interaction.user, member, minutes, reason)

    @app_commands.command(name="unmute", description="Remove timeout from a member")
    @app_commands.describe(member="Member to unmute")
    @mod_only()
    async def unmute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await member.timeout(None)
        await interaction.response.send_message(f"🔊 **{member}** has been unmuted.")
        log.info("%s unmuted %s", interaction.user, member)

    @app_commands.command(name="purge", description="Bulk-delete messages from this channel")
    @app_commands.describe(amount="Number of messages to delete (1–200)")
    @mod_only()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200] = 10) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True)
        log.info("%s purged %d messages in #%s", interaction.user, len(deleted), interaction.channel)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for warning")
    @mod_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        guild_id = interaction.guild_id
        self._warnings.setdefault(guild_id, {}).setdefault(member.id, []).append(reason)
        count = len(self._warnings[guild_id][member.id])
        try:
            await member.send(f"⚠️ You have been warned in **{interaction.guild.name}**.\n**Reason:** {reason}\n**Total warnings:** {count}")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(f"⚠️ **{member}** warned. Total warnings: **{count}**\n📝 Reason: {reason}")
        log.info("%s warned %s (total %d) — %s", interaction.user, member, count, reason)

    @app_commands.command(name="warnings", description="List all warnings for a member")
    @app_commands.describe(member="Member to check")
    @mod_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        warns = self._warnings.get(interaction.guild_id, {}).get(member.id, [])
        if not warns:
            await interaction.response.send_message(f"✅ **{member}** has no warnings.", ephemeral=True)
            return
        embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.orange())
        for i, reason in enumerate(warns, start=1):
            embed.add_field(name=f"Warning #{i}", value=reason, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = "❌ You don't have permission." if isinstance(error, app_commands.CheckFailure) else f"💥 Error: {error}"
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        log.error("Moderation cog error: %s", error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
