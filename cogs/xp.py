"""
cogs/xp.py – XP & leveling system.

Awards XP for messages, tracks levels, provides rank/leaderboard commands.
"""

import logging
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks

import settings
from data.store import store, xp_for_level
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
        log.info("Member scan complete — %d new, %d updated", total_new, total_updated)

    # ── Listener: award XP on message ────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots, DMs, and blacklisted channels
        if message.author.bot:
            return
        if message.guild is None:
            return
        if message.channel.id in settings.XP_BLACKLISTED_CHANNELS:
            return

        # Calculate XP
        amount = settings.XP_PER_MESSAGE + random.randint(
            settings.XP_BONUS_MIN, settings.XP_BONUS_MAX
        )

        # Server boosters get a multiplier
        if hasattr(message.author, "premium_since") and message.author.premium_since:
            amount = int(amount * settings.BOOSTER_XP_MULTIPLIER)

        new_xp, new_level, leveled_up = await store.add_xp(
            message.guild.id, message.author.id, amount
        )

        if leveled_up and settings.ANNOUNCE_LEVEL_UP:
            # Award level-up KCoins
            reward = settings.LEVEL_UP_REWARD
            new_balance = await store.add_balance(
                message.guild.id, message.author.id, reward
            )

            channel = message.channel
            if settings.LEVEL_UP_CHANNEL_ID:
                ch = message.guild.get_channel(settings.LEVEL_UP_CHANNEL_ID)
                if ch:
                    channel = ch

            gid = message.guild.id
            embed = discord.Embed(
                title=t(gid, "xp.level_up.title"),
                description=t(gid, "xp.level_up.desc",
                    user=message.author.mention, level=new_level,
                    xp=f"{new_xp:,}", next_xp=f"{xp_for_level(new_level + 1):,}",
                    symbol=settings.CURRENCY_SYMBOL, reward=f"{reward:,}",
                    currency=settings.CURRENCY_NAME, balance=f"{new_balance:,}"),
                color=discord.Color.gold(),
            )
            await channel.send(embed=embed)

    # ── /rank ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="rank", description="View your XP rank and level")
    @app_commands.describe(member="Member to check (defaults to yourself)")
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        member = member or interaction.user
        gid = interaction.guild_id
        user = store.get_user(gid, member.id)

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
            (i + 1 for i, (uid, _) in enumerate(all_users) if uid == str(member.id)),
            len(all_users),
        )

        uid = interaction.user.id
        embed = discord.Embed(
            title=tp(gid, uid, "xp.rank.title", name=member.display_name),
            color=member.color if member.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name=tp(gid, uid, "xp.rank.level"), value=f"**{current_level}**", inline=True)
        embed.add_field(name="XP", value=f"`{current_xp:,}`", inline=True)
        embed.add_field(name=tp(gid, uid, "xp.rank.rank"), value=f"#{rank_pos}", inline=True)
        embed.add_field(
            name=tp(gid, uid, "xp.rank.progress"),
            value=f"{bar} `{xp_in_level:,}/{xp_needed:,}`",
            inline=False,
        )
        embed.add_field(
            name=tp(gid, uid, "xp.rank.messages"),
            value=f"`{user['messages']:,}`",
            inline=True,
        )
        embed.add_field(
            name=settings.CURRENCY_NAME,
            value=f"{settings.CURRENCY_SYMBOL} `{user['balance']:,}`",
            inline=True,
        )
        await interaction.response.send_message(embed=embed)

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

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
            lines.append(
                f"{prefix} **{name}** — Lvl `{data['level']}` · `{data['xp']:,}` XP"
            )

        embed = discord.Embed(
            title=t(gid, "xp.lb.title"),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ── /setxp (admin) ────────────────────────────────────────────────────────
    @app_commands.command(name="setxp", description="Set a user's XP (Admin only)")
    @app_commands.describe(member="Target member", amount="XP amount to set")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        await store.set_xp(gid, member.id, amount)
        user = store.get_user(gid, member.id)
        await interaction.response.send_message(
            tp(gid, uid, "xp.setxp.done", name=member.display_name, xp=f"{user['xp']:,}", level=user['level']),
            ephemeral=True,
        )
        log.info("%s set %s XP to %d", interaction.user, member, amount)

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
