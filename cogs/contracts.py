"""
cogs/contracts.py – Player-to-player contract system (Discord side).

Contracts are **created in the KSP mod or on the website**, never here: the old
`/contract` and `/flagcontract` commands were retired because a contract written
from Discord could not carry the half that matters — the craft on the build stage,
the mod list, the orbit and Δv margins a mission is judged against. What is left
here is the part Discord is actually good at: the offer/dispute/review buttons that
land in a DM (see `cogs/contract_views.py`), the mod clean-up tool, and the rescue
leaderboard. `contract_actions.py` is the shared service all front ends call.
"""
import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import settings
from cogs import targets
from data.store import store, _db
from data import contracts as cdb
from i18n import t, tp, S
import contract_actions as ca

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
    "ct.moretime_request":{"en": "Time Extension Request"},
    "ct.moretime_desc":  {"en": "**{name}** is requesting a deadline extension.\nCurrent: **{old}** → New: **{new}**"},
    # Rescue stats / leaderboard
    "rescue.stat.title":  {"en": "🛟 {name}'s Rescues"},
    "rescue.stat.desc":   {"en": "Completed rescue missions: **{count}**"},
    "rescue.lb.title":    {"en": "🛟 Rescue Leaderboard"},
    "rescue.lb.empty":    {"en": "No rescues completed yet. Be the first to bring someone home!"},
    "rescue.lb.line":     {"en": "{prefix} **{name}** · `{count}` rescue(s)"},
})


class Contracts(commands.Cog, name="Contracts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dispute_timeout_loop.start()
        self.overdue_loop.start()
        self.gift_sweep_loop.start()

    def cog_unload(self):
        self.dispute_timeout_loop.cancel()
        self.overdue_loop.cancel()
        self.gift_sweep_loop.cancel()

    @app_commands.command(name="contractreset", description="[MOD] Cancel all active contracts for a user")
    @app_commands.describe(user="The user whose contracts should be cancelled",
                           username=targets.USERNAME_DESC)
    @app_commands.default_permissions(manage_guild=True)
    @targets.username_param
    async def contractreset(self, interaction: discord.Interaction,
                            user: discord.Member | None = None,
                            username: str | None = None):
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            tgt = await targets.resolve(interaction, user, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return

        active_statuses = {cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.MOD_REVIEW}
        cancelled = 0
        refunded = 0

        for c in await asyncio.to_thread(cdb.iter_user_contracts, gid, tgt.account_id):
            if c.get("status") in active_statuses:
                cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
                # Refund escrow to issuer (if issuer is not the bot)
                if str(c.get("issuer_id")) != str(interaction.client.user.id):
                    # Not int(): an issuer_id is an account id, and a website-only
                    # issuer's is "a_…". Casting it raised ValueError part-way
                    # through the loop, leaving the contracts already cancelled
                    # above it unrefunded.
                    await store.add_balance(
                        gid, str(c["issuer_id"]), c["payment"],
                        category=store.TX_CONTRACT_REFUND,
                        detail="Contracts reset by a moderator")
                    refunded += c["payment"]
                # A rescue's wreck was deleted from the issuer's save when the contract
                # was created. Wiping the contract without this leaves their ship gone
                # for good, with nothing left pointing at the snapshot that could
                # restore it. This bulk tool cancels statuses `contract_actions.cancel`
                # deliberately refuses (submitted, disputed, mod_review), so it cannot
                # simply delegate — but it owes the same clean-up.
                await ca.restore_rescue(gid, c["contract_id"], c)
                cancelled += 1

        # Also clear weekly mission selections for this user
        sel_col = _db.collection("guilds").document(str(gid)).collection("weekly_selections")
        selections_cleared = 0
        for doc in await asyncio.to_thread(lambda: list(sel_col.stream())):
            d = doc.to_dict()
            if d.get("user_id") == tgt.account_id:
                doc.reference.delete()
                selections_cleared += 1

        sym = settings.CURRENCY_SYMBOL
        await interaction.followup.send(
            f"✅ Cancelled **{cancelled}** contract(s) for {tgt.mention}. "
            f"Refunded **{refunded}** {sym}. Cleared **{selections_cleared}** mission selection(s).",
            ephemeral=True,
        )
        log.info("%s reset contracts for %s (%s): %d cancelled, %d refunded, %d selections cleared",
                 interaction.user, tgt.label, tgt.account_id,
                 cancelled, refunded, selections_cleared)

    # ── Background: close disputes nobody resolved ───────────────────────────
    #
    # A dispute is the one state with no natural end. Every other status is driven
    # forward by somebody who wants something — but a contractor who owes a fine wants
    # exactly nothing to happen, and before this loop nothing ever did.
    #
    # Half-hourly rather than by-the-minute: the deadline is measured in days, so the
    # worst case is being fined 30 minutes late, and a tighter loop would just re-query
    # Firestore for no one's benefit.
    @tasks.loop(minutes=30)
    async def dispute_timeout_loop(self):
        try:
            disputed = await asyncio.to_thread(cdb.list_by_status, cdb.DISPUTED)
        except Exception as exc:
            log.error("Dispute sweep could not list contracts: %s", exc)
            return

        now = datetime.utcnow()
        for c in disputed:
            deadline = ca.auto_fine_at(c)
            # None means the dispute predates the clock. expire_dispute stamps it and
            # returns without charging, so those get a full window from now.
            if deadline is not None and now < deadline:
                continue
            try:
                await ca.expire_dispute(int(c.get("guild_id") or 0), c["contract_id"])
            except Exception as exc:
                # One bad contract must not stop the sweep for every other one.
                log.error("Dispute sweep failed on %s: %s", c.get("contract_id"), exc)

    @dispute_timeout_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ── Background: start the clock on contracts nobody submitted ────────────
    #
    # DISPUTED was the only status ever swept, so an ACTIVE contract whose deadline
    # passed sat there forever — the contractor had stopped answering, and the issuer
    # could only withdraw it, which cost the contractor nothing. `expire_overdue`
    # charges nothing either; it moves the contract into dispute, where the contractor
    # still has every option and the existing auto-fine clock provides the ending.
    #
    # Daily rather than half-hourly: the grace period is measured in days, so a sweep
    # every 30 minutes would re-read every active contract in the system 48 times to
    # find the same handful.
    @tasks.loop(hours=24)
    async def overdue_loop(self):
        try:
            active = await asyncio.to_thread(cdb.list_by_status, cdb.ACTIVE)
        except Exception as exc:
            log.error("Overdue sweep could not list contracts: %s", exc)
            return

        for c in active:
            try:
                await ca.expire_overdue(int(c.get("guild_id") or 0), c["contract_id"])
            except Exception as exc:
                # One bad contract must not stop the sweep for every other one.
                log.error("Overdue sweep failed on %s: %s", c.get("contract_id"), exc)

    @overdue_loop.before_loop
    async def _wait_ready_overdue(self):
        await self.bot.wait_until_ready()

    # Quicksend payloads whose offer nobody ever answered. Their files are
    # otherwise only removed by the recipient's accept or decline, and the upload
    # quota is a daily rate, so an offer to an account that never polls was
    # storage that grew forever. Daily: one Storage list per thousand objects.
    @tasks.loop(hours=24)
    async def gift_sweep_loop(self):
        from data import imports as imp
        days = int(getattr(settings, "GIFT_FILE_MAX_AGE_DAYS", 30) or 30)
        try:
            await asyncio.to_thread(imp.sweep_stale_gift_files, days)
        except Exception as exc:
            log.error("Gift file sweep failed: %s", exc)

    @gift_sweep_loop.before_loop
    async def _wait_ready_gifts(self):
        await self.bot.wait_until_ready()

    # ── /rescues ────────────────────────────────────────────────────────────
    @app_commands.command(name="rescues", description="Show how many rescue missions a user has completed")
    @app_commands.describe(user="User to look up (defaults to yourself)",
                           username=targets.USERNAME_DESC)
    @targets.username_param
    async def rescues(self, interaction: discord.Interaction,
                      user: discord.Member | None = None,
                      username: str | None = None):
        gid = interaction.guild_id
        own = user is None and not (username or "").strip()
        await interaction.response.defer(ephemeral=own)
        try:
            tgt = await targets.resolve(interaction, user, username, default_self=True)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        count = store.get_user(gid, tgt.account_id).get("rescues", 0)
        embed = discord.Embed(
            title=tp(gid, interaction.user.id, "rescue.stat.title", name=tgt.label),
            description=tp(gid, interaction.user.id, "rescue.stat.desc", count=count),
            color=discord.Color.blue(),
        )
        if tgt.avatar_url:
            embed.set_thumbnail(url=tgt.avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=own)

    # ── /rescueboard ────────────────────────────────────────────────────────
    @app_commands.command(name="rescueboard", description="View the rescue-mission leaderboard")
    async def rescueboard(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        lb = [(uid, d) for uid, d in store.leaderboard(gid, key="rescues", limit=9999)
              if d.get("rescues", 0) > 0][:settings.LEADERBOARD_PAGE_SIZE]
        if not lb:
            await interaction.response.send_message(t(gid, "rescue.lb.empty"), ephemeral=True)
            return
        await targets.prefetch_names(uid for uid, _ in lb)

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            name = targets.board_name(interaction.guild, uid)
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

