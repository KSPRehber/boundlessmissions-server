"""
cogs/contracts.py – Player-to-player contract system.

/g contract @user "mission" money date fine
"""
import logging
import discord
from discord import app_commands
from discord.ext import commands

import settings
from data.store import store, _db
from data import contracts as cdb
from i18n import t, tp, S
from cogs.contract_views import (
    ContractOfferView, ContractWorkView, ContractReviewView,
    DisputeView, SettleApprovalView, ModReviewView, _embed,
)

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "ct.title":          {"en": "Contract"},
    "ct.mission":        {"en": "📋 Mission"},
    "ct.issuer":         {"en": "👤 Issuer"},
    "ct.contractor":     {"en": "🔧 Contractor"},
    "ct.payment":        {"en": "💰 Payment"},
    "ct.fine":           {"en": "⚠️ Fine"},
    "ct.due":            {"en": "📅 Due"},
    "ct.status":         {"en": "📌 Status"},
    "ct.review_title":   {"en": "Submission Review"},
    "ct.accepted":       {"en": "Contract Accepted!"},
    "ct.accepted_desc":  {"en": "**{payment}** {sym} transferred to your account."},
    "ct.disputed":       {"en": "Submission Refused"},
    "ct.disputed_desc":  {"en": "The other party refused your submission. Use one of the options below."},
    "ct.settle_request": {"en": "Settlement Request"},
    "ct.settle_desc":    {"en": "**{name}** is requesting a settlement (no exchange)."},
    "ct.settle_sent":    {"en": "✅ Settlement request sent."},
    "ct.settled":        {"en": "Settled. Escrow refunded."},
    "ct.settle_refused": {"en": "Settlement refused."},
    "ct.mod_review":     {"en": "Mod Review"},
    "ct.sued":           {"en": "⚖️ Case escalated to moderators."},
    "ct.fine_paid":      {"en": "Fine paid."},
    "ct.no_funds":       {"en": "❌ Insufficient balance."},
    "ct.offer_dm":       {"en": "📜 You received a new contract offer!"},
    "ct.created":        {"en": "✅ Contract created and sent to {name}."},
    # Flag-design contracts
    "ct.flag_mission":   {"en": "🚩 Flag design: {title}"},
    "ct.flag_offer_dm":  {"en": "🚩 You received a new flag-design request!"},
    "ct.flag_created":   {"en": "✅ Flag-design contract created and sent to {name}."},
    "ct.err_self":       {"en": "❌ You can't contract yourself."},
    "ct.err_funds":      {"en": "❌ Insufficient balance ({need} {sym} required)."},
    "ct.err_limit":      {"en": "❌ Active contract limit reached ({max})."},
    "ct.err_dm":         {"en": "❌ Could not DM the user."},
    "ct.err_date":       {"en": "❌ Invalid date format. Use YYYY-MM-DD."},
    "ct.moretime_request":{"en": "Time Extension Request"},
    "ct.moretime_desc":  {"en": "**{name}** is requesting a deadline extension.\nCurrent: **{old}** → New: **{new}**"},
    # Rescue stats / leaderboard
    "rescue.stat.title":  {"en": "🛟 {name}'s Rescues"},
    "rescue.stat.desc":   {"en": "Completed rescue missions: **{count}**"},
    "rescue.lb.title":    {"en": "🛟 Rescue Leaderboard"},
    "rescue.lb.empty":    {"en": "No rescues completed yet — be the first to bring someone home!"},
    "rescue.lb.line":     {"en": "{prefix} **{name}** — `{count}` rescue(s)"},
})


class Contracts(commands.Cog, name="Contracts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="contract", description="Send a contract to another user")
    @app_commands.describe(
        user="User to send the contract to",
        mission="Mission description",
        money="Payment amount in KCoins",
        date_due="Due date (YYYY-MM-DD)",
        fine="Fine amount if contract is breached",
    )
    async def contract(
        self, interaction: discord.Interaction,
        user: discord.Member, mission: str,
        money: int, date_due: str, fine: int,
    ):
        gid = interaction.guild_id
        uid = interaction.user.id
        sym = settings.CURRENCY_SYMBOL

        # Validations (fast — before defer)
        if user.id == uid and not settings.CONTRACT_ALLOW_SELF:
            await interaction.response.send_message(tp(gid, uid, "ct.err_self"), ephemeral=True)
            return

        # Validate date
        try:
            from datetime import datetime, date
            dt = datetime.strptime(date_due, "%Y-%m-%d").date()
            if dt <= date.today():
                await interaction.response.send_message(tp(gid, uid, "ct.err_date"), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(tp(gid, uid, "ct.err_date"), ephemeral=True)
            return

        # Defer immediately — Firestore queries below can be slow
        await interaction.response.defer(ephemeral=True)

        # Check balance (need enough for escrow)
        bal = store.get_user(gid, uid)["balance"]
        if bal < money:
            await interaction.followup.send(
                tp(gid, uid, "ct.err_funds", need=money, sym=sym), ephemeral=True)
            return

        # Check contract limit
        count = cdb.count_active(gid, uid)
        if count >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
            await interaction.followup.send(
                tp(gid, uid, "ct.err_limit", max=settings.MAX_ACTIVE_CONTRACTS_PER_USER), ephemeral=True)
            return

        # Escrow: lock the payment
        await store.add_balance(gid, uid, -money)

        # Create contract
        c = cdb.create_contract(
            gid, uid, interaction.user.display_name,
            user.id, user.display_name,
            mission, money, fine, date_due,
        )

        # DM the contractor
        try:
            e = _embed(c, gid)
            e.description = t(gid, "ct.offer_dm")
            view = ContractOfferView(c["contract_id"], gid)
            dm_msg = await user.send(embed=e, view=view)
            cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except discord.Forbidden:
            # Refund if can't DM
            await store.add_balance(gid, uid, money)
            cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
            await interaction.followup.send(tp(gid, uid, "ct.err_dm"), ephemeral=True)
            return

        await interaction.followup.send(
            tp(gid, uid, "ct.created", name=user.display_name), ephemeral=True)

    @app_commands.command(name="flagcontract", description="Request a custom flag design from another user")
    @app_commands.describe(
        user="User you want to design the flag",
        title="What the flag should depict / be called",
        money="Payment amount in KCoins",
        date_due="Due date (YYYY-MM-DD)",
        fine="Fine amount if the contract is breached",
    )
    async def flagcontract(
        self, interaction: discord.Interaction,
        user: discord.Member, title: str,
        money: int, date_due: str, fine: int,
    ):
        gid = interaction.guild_id
        uid = interaction.user.id
        sym = settings.CURRENCY_SYMBOL

        if user.id == uid and not settings.CONTRACT_ALLOW_SELF:
            await interaction.response.send_message(tp(gid, uid, "ct.err_self"), ephemeral=True)
            return

        # Validate date (must be in the future)
        try:
            from datetime import datetime, date
            dt = datetime.strptime(date_due, "%Y-%m-%d").date()
            if dt <= date.today():
                await interaction.response.send_message(tp(gid, uid, "ct.err_date"), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(tp(gid, uid, "ct.err_date"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Check balance (escrow) and active-contract limit
        bal = store.get_user(gid, uid)["balance"]
        if bal < money:
            await interaction.followup.send(
                tp(gid, uid, "ct.err_funds", need=money, sym=sym), ephemeral=True)
            return

        if cdb.count_active(gid, uid) >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
            await interaction.followup.send(
                tp(gid, uid, "ct.err_limit", max=settings.MAX_ACTIVE_CONTRACTS_PER_USER), ephemeral=True)
            return

        # Escrow: lock the payment
        await store.add_balance(gid, uid, -money)

        c = cdb.create_contract(
            gid, uid, interaction.user.display_name,
            user.id, user.display_name,
            t(gid, "ct.flag_mission", title=title), money, fine, date_due,
            mission_type=cdb.FLAG_DESIGN,
        )

        # DM the designer with the offer
        try:
            e = _embed(c, gid)
            e.description = t(gid, "ct.flag_offer_dm")
            view = ContractOfferView(c["contract_id"], gid)
            dm_msg = await user.send(embed=e, view=view)
            cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except discord.Forbidden:
            await store.add_balance(gid, uid, money)
            cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
            await interaction.followup.send(tp(gid, uid, "ct.err_dm"), ephemeral=True)
            return

        await interaction.followup.send(
            tp(gid, uid, "ct.flag_created", name=user.display_name), ephemeral=True)

    @app_commands.command(name="contractreset", description="[MOD] Cancel all active contracts for a user")
    @app_commands.describe(user="The user whose contracts should be cancelled")
    @app_commands.default_permissions(manage_guild=True)
    async def contractreset(self, interaction: discord.Interaction, user: discord.Member):
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        col = cdb._col(gid)
        active_statuses = [cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.MOD_REVIEW]
        cancelled = 0
        refunded = 0

        for doc in col.where("status", "in", active_statuses).stream():
            c = doc.to_dict()
            uid_str = str(user.id)
            if c.get("issuer_id") == uid_str or c.get("contractor_id") == uid_str:
                cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
                # Refund escrow to issuer (if issuer is not the bot)
                if str(c.get("issuer_id")) != str(interaction.client.user.id):
                    await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
                    refunded += c["payment"]
                cancelled += 1

        # Also clear weekly mission selections for this user
        sel_col = _db.collection("guilds").document(str(gid)).collection("weekly_selections")
        selections_cleared = 0
        for doc in sel_col.stream():
            d = doc.to_dict()
            if d.get("user_id") == str(user.id):
                doc.reference.delete()
                selections_cleared += 1

        sym = settings.CURRENCY_SYMBOL
        await interaction.followup.send(
            f"✅ Cancelled **{cancelled}** contract(s) for {user.mention}. "
            f"Refunded **{refunded}** {sym}. Cleared **{selections_cleared}** mission selection(s).",
            ephemeral=True,
        )
        log.info("%s reset contracts for %s: %d cancelled, %d refunded, %d selections cleared",
                 interaction.user, user, cancelled, refunded, selections_cleared)

    # ── /rescues ────────────────────────────────────────────────────────────
    @app_commands.command(name="rescues", description="Show how many rescue missions a user has completed")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def rescues(self, interaction: discord.Interaction, user: discord.Member | None = None):
        gid = interaction.guild_id
        target = user or interaction.user
        count = store.get_user(gid, target.id).get("rescues", 0)
        embed = discord.Embed(
            title=tp(gid, interaction.user.id, "rescue.stat.title", name=target.display_name),
            description=tp(gid, interaction.user.id, "rescue.stat.desc", count=count),
            color=discord.Color.blue(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=user is None)

    # ── /rescueboard ────────────────────────────────────────────────────────
    @app_commands.command(name="rescueboard", description="View the rescue-mission leaderboard")
    async def rescueboard(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        lb = [(uid, d) for uid, d in store.leaderboard(gid, key="rescues", limit=9999)
              if d.get("rescues", 0) > 0][:settings.LEADERBOARD_PAGE_SIZE]
        if not lb:
            await interaction.response.send_message(t(gid, "rescue.lb.empty"), ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
            lines.append(t(gid, "rescue.lb.line", prefix=prefix, name=name, count=data.get("rescues", 0)))

        embed = discord.Embed(
            title=t(gid, "rescue.lb.title"),
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Contracts(bot))
    # Register DynamicItem button classes — regex-matched, survives restarts
    from cogs.contract_views import ALL_DYNAMIC_ITEMS
    bot.add_dynamic_items(*ALL_DYNAMIC_ITEMS)

