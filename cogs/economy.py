"""
cogs/economy.py – Economy system (KCoins).

Commands for checking balance, paying other users, and mod tools
for giving/fining/setting balances.
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

import settings
from cogs import perms
from data.store import store
from i18n import t, tp

log = logging.getLogger(__name__)


def mod_only():
    """User must have Kick Members or Administrator permission. Gates on the real
    invoker (mimic-safe)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        u = perms.real_user(interaction)
        if isinstance(u, discord.Member):
            return (u.guild_permissions.kick_members
                    or u.guild_permissions.administrator)
        return False
    return app_commands.check(predicate)


class Economy(commands.Cog, name="Economy"):
    """KCoins economy system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /balance ──────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your or another user's KCoin balance")
    @app_commands.describe(member="Member to check (defaults to yourself)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        gid = interaction.guild_id
        uid = interaction.user.id
        user = store.get_user(gid, member.id)

        embed = discord.Embed(
            title=tp(gid, uid, "eco.balance.title", symbol=settings.CURRENCY_SYMBOL, name=member.display_name),
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name=settings.CURRENCY_NAME,
            value=f"**{user['balance']:,}**",
            inline=True,
        )
        embed.add_field(name=tp(gid, uid, "xp.rank.level"), value=f"**{user['level']}**", inline=True)
        embed.set_footer(text=tp(gid, uid, "eco.balance.footer", currency=settings.CURRENCY_NAME))
        await interaction.response.send_message(embed=embed)

    # ── /pay ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="pay", description="Transfer KCoins to another user")
    @app_commands.describe(member="Who to pay", amount="Amount to transfer")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        # Validation
        if member.bot:
            await interaction.response.send_message(tp(gid, uid, "eco.pay.cant_bot"), ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(tp(gid, uid, "eco.pay.cant_self"), ephemeral=True)
            return
        if amount < settings.MIN_TRANSFER:
            await interaction.response.send_message(
                tp(gid, uid, "eco.pay.min", min=f"{settings.MIN_TRANSFER:,}", currency=settings.CURRENCY_NAME),
                ephemeral=True,
            )
            return

        # Execute transfer. Atomic debit so two concurrent /pay calls can't both
        # pass the balance check on the same funds and transfer more than is held.
        if not await store.try_debit(gid, interaction.user.id, amount):
            sender = store.get_user(gid, interaction.user.id)
            await interaction.response.send_message(
                tp(gid, uid, "eco.pay.insufficient", balance=f"{sender['balance']:,}", currency=settings.CURRENCY_NAME),
                ephemeral=True,
            )
            return

        new_receiver_bal = await store.add_balance(gid, member.id, amount)
        new_sender_bal = store.get_user(gid, interaction.user.id)["balance"]

        embed = discord.Embed(
            title=t(gid, "eco.pay.title", symbol=settings.CURRENCY_SYMBOL),
            description=t(gid, "eco.pay.desc",
                sender=interaction.user.display_name, receiver=member.display_name,
                amount=f"{amount:,}", currency=settings.CURRENCY_NAME),
            color=discord.Color.green(),
        )
        embed.add_field(
            name=f"{interaction.user.display_name}",
            value=f"`{new_sender_bal:,}`",
            inline=True,
        )
        embed.add_field(
            name=f"{member.display_name}",
            value=f"`{new_receiver_bal:,}`",
            inline=True,
        )
        await interaction.response.send_message(embed=embed)
        log.info("%s paid %s %d KCoins", interaction.user, member, amount)

    # ── /richest ──────────────────────────────────────────────────────────────
    @app_commands.command(name="richest", description="View the wealthiest members")
    async def richest(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        lb = store.leaderboard(gid, key="balance")
        if not lb:
            await interaction.response.send_message(t(gid, "eco.richest.empty"), ephemeral=True)
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, data) in enumerate(lb):
            if data.get("balance", 0) == 0:
                continue
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
            lines.append(f"{prefix} **{name}** · {settings.CURRENCY_SYMBOL} `{data['balance']:,}`")

        if not lines:
            await interaction.response.send_message(t(gid, "eco.richest.empty"), ephemeral=True)
            return

        embed = discord.Embed(
            title=t(gid, "eco.richest.title", symbol=settings.CURRENCY_SYMBOL),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    #  MOD-ONLY COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    # ── /givemoney ────────────────────────────────────────────────────────────
    @app_commands.command(name="givemoney", description="Give KCoins to a user (Mod only)")
    @app_commands.describe(member="Who to give to", amount="Amount to give", reason="Reason (optional)")
    @app_commands.default_permissions(kick_members=True)
    @mod_only()
    async def givemoney(
        self, interaction: discord.Interaction, member: discord.Member,
        amount: int, reason: str = "No reason provided",
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount <= 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_positive"), ephemeral=True)
            return

        new_bal = await store.add_balance(gid, member.id, amount)
        embed = discord.Embed(
            title=t(gid, "eco.give.title", symbol=settings.CURRENCY_SYMBOL),
            description=t(gid, "eco.give.desc",
                name=member.display_name, amount=f"{amount:,}",
                currency=settings.CURRENCY_NAME, reason=reason, balance=f"{new_bal:,}"),
            color=discord.Color.green(),
        )
        embed.set_footer(text=t(gid, "common.issued_by", name=interaction.user.display_name))
        await interaction.response.send_message(embed=embed)
        log.info("%s gave %s %d KCoins — %s", interaction.user, member, amount, reason)

    # ── /fine ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="fine", description="Deduct KCoins from a user (Mod only)")
    @app_commands.describe(member="Who to fine", amount="Amount to deduct", reason="Reason (optional)")
    @app_commands.default_permissions(kick_members=True)
    @mod_only()
    async def fine(
        self, interaction: discord.Interaction, member: discord.Member,
        amount: int, reason: str = "No reason provided",
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount <= 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_positive"), ephemeral=True)
            return

        new_bal = await store.add_balance(gid, member.id, -amount)
        embed = discord.Embed(
            title=t(gid, "eco.fine.title"),
            description=t(gid, "eco.fine.desc",
                name=member.display_name, amount=f"{amount:,}",
                currency=settings.CURRENCY_NAME, reason=reason, balance=f"{new_bal:,}"),
            color=discord.Color.red(),
        )
        embed.set_footer(text=t(gid, "common.issued_by", name=interaction.user.display_name))
        await interaction.response.send_message(embed=embed)

        # DM the fined user
        try:
            await member.send(t(gid, "eco.fine.dm",
                guild=interaction.guild.name, amount=f"{amount:,}",
                currency=settings.CURRENCY_NAME, reason=reason))
        except discord.Forbidden:
            pass
        log.info("%s fined %s %d KCoins — %s", interaction.user, member, amount, reason)

    # ── /setbalance ───────────────────────────────────────────────────────────
    @app_commands.command(name="setbalance", description="Set a user's KCoin balance (Mod only)")
    @app_commands.describe(member="Target member", amount="New balance amount")
    @app_commands.default_permissions(kick_members=True)
    @mod_only()
    async def setbalance(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount < 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_negative"), ephemeral=True)
            return

        # Set directly via store
        async with store._lock:
            user = store.get_user(gid, member.id)
            user["balance"] = amount
            store._mark_dirty(gid, member.id)

        await interaction.response.send_message(
            tp(gid, uid, "eco.setbal.done", name=member.display_name, amount=f"{amount:,}", currency=settings.CURRENCY_NAME),
            ephemeral=True,
        )
        log.info("%s set %s balance to %d", interaction.user, member, amount)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(tp(gid, uid, "common.no_perm"), ephemeral=True)
        else:
            log.error("Economy cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(tp(gid, uid, "common.error"), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
