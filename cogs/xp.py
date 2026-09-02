"""
cogs/xp.py – XP & leveling commands.

Levels, rank and the leaderboard. XP is no longer earned by talking: the
message listener that used to award it is gone, and every award now comes from
something flown — a completed contract, an analysed screenshot, a weekly
mission — through `rewards.grant_xp`. This cog only reads that state (plus the
admin setter and the auto-save loop that flushes the store).
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks

import settings
from cogs import perms, targets
from data.store import store, xp_for_level
from data import guild_config
from i18n import t, tp, load_all_langs

log = logging.getLogger(__name__)


class XP(commands.Cog, name="XP"):
    """XP tracking and leveling system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Called when the cog is loaded — start background tasks."""
        await store.load()
        load_all_langs()
        guild_config.load()
        self.auto_save.start()
        self.scan_members_loop.start()

    async def cog_unload(self) -> None:
        """Called when the cog is unloaded — save and stop tasks."""
        self.auto_save.cancel()
        self.scan_members_loop.cancel()
        await store.save()

    # ── Background: auto-save ────────────────────────────────────────────────
    @tasks.loop(seconds=settings.AUTO_SAVE_INTERVAL)
    async def auto_save(self) -> None:
        # Re-read the wallet first if a budget freeze was the only thing that
        # stopped it loading at boot. This loop is the heartbeat that already
        # exists, and without a retry the read-only state outlives its cause: the
        # guard resets itself on the UTC month rollover, so the bot would go on
        # refusing every write long after the freeze was gone, with nobody in the
        # loop to notice. A no-op unless `budget_blocked` and the guard has
        # actually dropped below FROZEN.
        try:
            await store.ensure_loaded()
        except Exception as exc:                    # never let this kill the loop
            log.warning("Wallet reload attempt failed: %s", exc)
        await store.save_if_dirty()

    # ── Background: scan all members ─────────────────────────────────────────
    @tasks.loop(minutes=15)
    async def scan_members_loop(self) -> None:
        """Ensure every guild member has a record in the store."""
        await self._scan_all_members()

    @scan_members_loop.before_loop
    async def before_scan(self) -> None:
        """Wait until the bot is fully connected before scanning."""
        await self.bot.wait_until_ready()

    # Also run on first connect
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._scan_all_members()

    async def _scan_all_members(self) -> None:
        """Iterate every guild and register/update all members."""
        total_new = 0
        total_updated = 0
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot:
                    continue
                user = store.get_user(guild.id, member.id)
                changed = False

                # Stamp identity fields
                if user.get("user_id") != str(member.id):
                    user["user_id"] = str(member.id)
                    changed = True
                # Keep username current
                current_name = member.name
                if user.get("username") != current_name:
                    user["username"] = current_name
                    changed = True
                # Stamp join date if missing
                if not user.get("joined_at") and member.joined_at:
                    user["joined_at"] = member.joined_at.isoformat()
                    changed = True
                    total_new += 1

                if changed:
                    store._mark_dirty(guild.id, member.id)
                    total_updated += 1
        if total_updated:
            await store.save_if_dirty()
        log.info("Member scan complete: %d new, %d updated", total_new, total_updated)

    # ── /rank ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="rank", description="View your XP rank and level")
    @app_commands.describe(member="Member to check (defaults to yourself)",
                           username=targets.USERNAME_DESC)
    @targets.username_param
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        username: str | None = None,
    ) -> None:
        gid = interaction.guild_id
        await interaction.response.defer()
        try:
            tgt = await targets.resolve(interaction, member, username, default_self=True)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        user = store.get_user(gid, tgt.account_id)

        current_level = user["level"]
        current_xp = user["xp"]
        xp_current_level = xp_for_level(current_level)
        xp_next_level = xp_for_level(current_level + 1)
        xp_in_level = current_xp - xp_current_level
        xp_needed = xp_next_level - xp_current_level

        # Progress bar
        progress = xp_in_level / xp_needed if xp_needed > 0 else 1.0
        bar_len = 10
        filled = int(bar_len * progress)
        bar = "🟩" * filled + "⬛" * (bar_len - filled)

        # Rank position
        all_users = store.leaderboard(gid, limit=9999)
        rank_pos = next(
            (i + 1 for i, (uid, _) in enumerate(all_users) if uid == tgt.account_id),
            len(all_users),
        )

        uid = interaction.user.id
        colour = discord.Color.blurple()
        if tgt.member is not None and tgt.member.color.value:
            colour = tgt.member.color
        embed = discord.Embed(
            title=tp(gid, uid, "xp.rank.title", name=tgt.label),
            color=colour,
        )
        if tgt.avatar_url:
            embed.set_thumbnail(url=tgt.avatar_url)
        embed.add_field(name=tp(gid, uid, "xp.rank.level"), value=f"**{current_level}**", inline=True)
        embed.add_field(name="XP", value=f"`{current_xp:,}`", inline=True)
        embed.add_field(name=tp(gid, uid, "xp.rank.rank"), value=f"#{rank_pos}", inline=True)
        embed.add_field(
            name=tp(gid, uid, "xp.rank.progress"),
            value=f"{bar} `{xp_in_level:,}/{xp_needed:,}`",
            inline=False,
        )
        embed.add_field(
            name=settings.CURRENCY_NAME,
            value=f"{settings.CURRENCY_SYMBOL} `{user['balance']:,}`",
            inline=True,
        )
        await interaction.followup.send(embed=embed)

    # ── /leaderboard ──────────────────────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="View the server XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        lb = store.leaderboard(gid)
        if not lb:
            await interaction.response.send_message(
                t(gid, "xp.lb.empty"), ephemeral=True
            )
            return
        await targets.prefetch_names(uid for uid, _ in lb)

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            name = targets.board_name(interaction.guild, uid)
            lines.append(
                f"{prefix} **{name}** · Lvl `{data['level']}` · `{data['xp']:,}` XP"
            )

        embed = discord.Embed(
            title=t(gid, "xp.lb.title"),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ── /setxp (admin) ────────────────────────────────────────────────────────
    @app_commands.command(name="setxp", description="Set a user's XP (Admin only)")
    @app_commands.describe(member="Target member", username=targets.USERNAME_DESC,
                           amount="XP amount to set")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @targets.username_param
    async def setxp(
        self,
        interaction: discord.Interaction,
        amount: int,
        member: discord.Member | None = None,
        username: str | None = None,
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True)
        try:
            tgt = await targets.resolve(interaction, member, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        # Guild-local authority, global records: refuse a target this
        # moderator's server does not cover. See `perms.moderatable_here`.
        _no = perms.moderatable_here(interaction, tgt)
        if _no:
            await interaction.edit_original_response(content=_no)
            return
        await store.set_xp(gid, tgt.account_id, amount)
        user = store.get_user(gid, tgt.account_id)
        await interaction.followup.send(
            tp(gid, uid, "xp.setxp.done", name=tgt.label, xp=f"{user['xp']:,}", level=user['level']),
            ephemeral=True,
        )
        log.info("%s set %s (%s) XP to %d",
                 interaction.user, tgt.label, tgt.account_id, amount)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                tp(gid, uid, "common.no_perm"), ephemeral=True
            )
        else:
            log.error("XP cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    tp(gid, uid, "common.error"), ephemeral=True
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(XP(bot))
