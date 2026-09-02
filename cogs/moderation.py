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


# Every action below is performed with the BOT's permissions, not the invoker's.
# Gating them all on one `is_mod_user` — which is satisfied by `kick_members` alone
# — therefore made Kick Members buy ban, timeout, purge and unban as well, erasing
# the separation Discord's own permission model exists to express. A junior helper
# given only Kick could remove senior staff.
#
# So each command now additionally requires the permission it actually performs.
NO_MENTIONS = discord.AllowedMentions.none()


def mod_only(*, needs: str | None = None):
    """Mod gate, plus the specific Discord permission this action performs.

    `needs` names a `discord.Permissions` flag the REAL invoker must hold (owner and
    guild administrators pass regardless). Without it, one gate spent on the whole
    cog meant the weakest moderation permission unlocked the strongest action.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if not perms.is_mod_user(interaction):   # mimic-safe, per-guild mod role
            return False
        if needs is None:
            return True
        u = perms.real_user(interaction)
        if not isinstance(u, discord.Member):
            return False
        p = u.guild_permissions
        if p.administrator or perms.is_owner_user(interaction):
            return True
        if getattr(p, needs, False):
            return True
        raise app_commands.CheckFailure(
            f"That action needs the **{needs.replace('_', ' ').title()}** permission "
            f"in this server, which your role does not have.")
    return app_commands.check(predicate)


def _outranks(interaction: discord.Interaction, member: discord.Member) -> str | None:
    """Why the invoker may not act on this member, or None.

    Discord enforces role hierarchy for actions a *user* takes; these are taken by
    the bot, so nothing stopped a moderator from banning someone above them — only
    from banning someone above the BOT. The guild owner is never actionable, and
    acting on yourself is refused because it is never what was meant.
    """
    actor = perms.real_user(interaction)
    if not isinstance(actor, discord.Member) or member is None:
        return None
    if member.id == actor.id:
        return "You cannot use a moderation command on yourself."
    guild = interaction.guild
    if guild is not None and member.id == guild.owner_id:
        return "That member owns this server; the bot will not act on them."
    if perms.is_owner_user(interaction) or actor.guild_permissions.administrator:
        return None
    if member.top_role >= actor.top_role:
        return (f"**{member.display_name}** has a role at or above yours, so you "
                f"cannot moderate them.")
    return None


async def _refuse(interaction: discord.Interaction, why: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(why, ephemeral=True)
    else:
        await interaction.response.send_message(why, ephemeral=True)


class Moderation(commands.Cog, name="Moderation"):
    """Server moderation tools."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._warnings: dict[int, dict[int, list[str]]] = {}

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason for kick")
    @mod_only(needs="kick_members")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        why = _outranks(interaction, member)
        if why:
            await _refuse(interaction, why)
            return
        await member.kick(reason=reason)
        # `allowed_mentions` pinned: `reason` is free text from the invoker and this
        # message is public, so without it "@everyone" in a reason pinged the server
        # from the bot's account, whatever permissions the invoker held.
        await interaction.response.send_message(f"👢 **{member}** has been kicked.\n📝 Reason: {reason}",
                                                allowed_mentions=NO_MENTIONS)
        log.info("%s kicked %s: %s", interaction.user, member, reason)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="Member to ban", reason="Reason for ban", delete_days="Days of messages to delete (0-7)")
    @mod_only(needs="ban_members")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        why = _outranks(interaction, member)
        if why:
            await _refuse(interaction, why)
            return
        await member.ban(reason=reason, delete_message_days=delete_days)
        await interaction.response.send_message(f"🔨 **{member}** has been banned.\n📝 Reason: {reason}",
                                                allowed_mentions=NO_MENTIONS)
        log.info("%s banned %s: %s", interaction.user, member, reason)

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason")
    @mod_only(needs="ban_members")
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided") -> None:
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)
            await interaction.response.send_message(f"✅ **{user}** has been unbanned.")
            log.info("%s unbanned %s: %s", interaction.user, user, reason)
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found or not banned.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)

    @app_commands.command(name="mute", description="Timeout (mute) a member")
    @app_commands.describe(member="Member to mute", minutes="Duration in minutes (1-40320)", reason="Reason")
    @mod_only(needs="moderate_members")
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320] = 10, reason: str = "No reason provided") -> None:
        why = _outranks(interaction, member)
        if why:
            await _refuse(interaction, why)
            return
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(f"🔇 **{member}** muted for **{minutes} min**.\n📝 Reason: {reason}",
                                                allowed_mentions=NO_MENTIONS)
        log.info("%s muted %s for %d min: %s", interaction.user, member, minutes, reason)

    @app_commands.command(name="unmute", description="Remove timeout from a member")
    @app_commands.describe(member="Member to unmute")
    @mod_only(needs="moderate_members")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        why = _outranks(interaction, member)
        if why:
            await _refuse(interaction, why)
            return
        await member.timeout(None)
        await interaction.response.send_message(f"🔊 **{member}** has been unmuted.")
        log.info("%s unmuted %s", interaction.user, member)

    @app_commands.command(name="purge", description="Bulk-delete messages from this channel")
    @app_commands.describe(amount="Number of messages to delete (1-200)")
    @mod_only(needs="manage_messages")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200] = 10) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True)
        log.info("%s purged %d messages in #%s", interaction.user, len(deleted), interaction.channel)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for warning")
    @mod_only(needs="moderate_members")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        why = _outranks(interaction, member)
        if why:
            await _refuse(interaction, why)
            return
        guild_id = interaction.guild_id
        self._warnings.setdefault(guild_id, {}).setdefault(member.id, []).append(reason)
        count = len(self._warnings[guild_id][member.id])
        try:
            await member.send(f"⚠️ You have been warned in **{interaction.guild.name}**.\n**Reason:** {reason}\n**Total warnings:** {count}")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(f"⚠️ **{member}** warned. Total warnings: **{count}**\n📝 Reason: {reason}",
                                                allowed_mentions=NO_MENTIONS)
        log.info("%s warned %s (total %d): %s", interaction.user, member, count, reason)

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
        # `mod_only` raises a CheckFailure that NAMES the missing permission, and this
        # used to overwrite it with a generic line — so a moderator who could ban
        # yesterday and cannot today was told the same thing as someone who is not a
        # moderator at all. Use the raised text when there is one; the generic line
        # stays as the fallback for a bare check with no message.
        if isinstance(error, app_commands.CheckFailure):
            msg = f"❌ {error}" if str(error) else "❌ You don't have permission."
        else:
            msg = f"💥 Error: {error}"
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        log.error("Moderation cog error: %s", error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
