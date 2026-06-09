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
    "ct.title":          {"tr": "Sözleşme", "en": "Contract"},
    "ct.mission":        {"tr": "📋 Görev", "en": "📋 Mission"},
    "ct.issuer":         {"tr": "👤 Veren", "en": "👤 Issuer"},
    "ct.contractor":     {"tr": "🔧 Yüklenici", "en": "🔧 Contractor"},
    "ct.payment":        {"tr": "💰 Ödeme", "en": "💰 Payment"},
    "ct.fine":           {"tr": "⚠️ Ceza", "en": "⚠️ Fine"},
    "ct.due":            {"tr": "📅 Teslim", "en": "📅 Due"},
    "ct.status":         {"tr": "📌 Durum", "en": "📌 Status"},
    "ct.review_title":   {"tr": "Teslimat İnceleme", "en": "Submission Review"},
    "ct.accepted":       {"tr": "Sözleşme Kabul Edildi!", "en": "Contract Accepted!"},
    "ct.accepted_desc":  {"tr": "**{payment}** {sym} hesabınıza aktarıldı.", "en": "**{payment}** {sym} transferred to your account."},
    "ct.disputed":       {"tr": "Teslimat Reddedildi", "en": "Submission Refused"},
    "ct.disputed_desc":  {"tr": "Karşı taraf teslimatınızı reddetti. Aşağıdaki seçeneklerden birini kullanın.", "en": "The other party refused your submission. Use one of the options below."},
    "ct.settle_request": {"tr": "Uzlaşma Teklifi", "en": "Settlement Request"},
    "ct.settle_desc":    {"tr": "**{name}** uzlaşma talep ediyor (karşılıklı ödeme yok).", "en": "**{name}** is requesting a settlement (no exchange)."},
    "ct.settle_sent":    {"tr": "✅ Uzlaşma teklifi gönderildi.", "en": "✅ Settlement request sent."},
    "ct.settled":        {"tr": "Uzlaşıldı. Emanet iade edildi.", "en": "Settled. Escrow refunded."},
    "ct.settle_refused": {"tr": "Uzlaşma reddedildi.", "en": "Settlement refused."},
    "ct.mod_review":     {"tr": "Mod İncelemesi", "en": "Mod Review"},
    "ct.sued":           {"tr": "⚖️ Dava moderatörlere iletildi.", "en": "⚖️ Case escalated to moderators."},
    "ct.fine_paid":      {"tr": "Ceza ödendi.", "en": "Fine paid."},
    "ct.no_funds":       {"tr": "❌ Yetersiz bakiye.", "en": "❌ Insufficient balance."},
    "ct.offer_dm":       {"tr": "📜 Yeni bir sözleşme teklifi aldınız!", "en": "📜 You received a new contract offer!"},
    "ct.created":        {"tr": "✅ Sözleşme oluşturuldu ve {name} adlı kullanıcıya gönderildi.", "en": "✅ Contract created and sent to {name}."},
    "ct.err_self":       {"tr": "❌ Kendinize sözleşme gönderemezsiniz.", "en": "❌ You can't contract yourself."},
    "ct.err_funds":      {"tr": "❌ Yetersiz bakiye ({need} {sym} gerekli).", "en": "❌ Insufficient balance ({need} {sym} required)."},
    "ct.err_limit":      {"tr": "❌ Aktif sözleşme limitine ulaşıldı ({max}).", "en": "❌ Active contract limit reached ({max})."},
    "ct.err_dm":         {"tr": "❌ Kullanıcıya DM gönderilemedi.", "en": "❌ Could not DM the user."},
    "ct.err_date":       {"tr": "❌ Geçersiz tarih formatı. YYYY-MM-DD kullanın.", "en": "❌ Invalid date format. Use YYYY-MM-DD."},
    "ct.moretime_request":{"tr": "Süre Uzatma Talebi", "en": "Time Extension Request"},
    "ct.moretime_desc":  {"tr": "**{name}** süre uzatması talep ediyor.\nMevcut: **{old}** → Yeni: **{new}**", "en": "**{name}** is requesting a deadline extension.\nCurrent: **{old}** → New: **{new}**"},
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Contracts(bot))
    # Register DynamicItem button classes — regex-matched, survives restarts
    from cogs.contract_views import ALL_DYNAMIC_ITEMS
    bot.add_dynamic_items(*ALL_DYNAMIC_ITEMS)

