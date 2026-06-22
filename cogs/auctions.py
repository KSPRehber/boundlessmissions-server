"""
cogs/auctions.py – Reverse (Dutch) auctions for contracts.

An issuer posts a mission with a STARTING price (escrowed up front). Contractors
bid the price DOWN via a modal; the lowest bid when the auction ends wins and is
bound to an ACTIVE contract for that amount. Leftover escrow is refunded.

Auctions end either when their timer elapses (a background loop closes them) or
when the issuer presses "End now". Bid/End buttons are DynamicItems, so they keep
working across restarts; the loop makes timed closes restart-safe too.
"""
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, DynamicItem

import settings
from data.store import store
from data import auctions as adb
from data import contracts as cdb
from i18n import t, tp, S
from cogs.contract_views import ContractWorkView, _embed

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "auc.title_open":      {"en": "🔨 Reverse Auction"},
    "auc.title_closed":    {"en": "🔨 Auction Closed"},
    "auc.title_cancelled": {"en": "🔨 Auction Cancelled"},
    "auc.mission":         {"en": "📋 Mission"},
    "auc.issuer":          {"en": "👤 Issuer"},
    "auc.start":           {"en": "🏷️ Starting Price"},
    "auc.current":         {"en": "📉 Lowest Bid"},
    "auc.nobids":          {"en": "No bids yet. Be the first to undercut!"},
    "auc.bidder":          {"en": "by {name}"},
    "auc.bids":            {"en": "🔁 Bids"},
    "auc.ends":            {"en": "⏳ Ends"},
    "auc.due":             {"en": "📅 Contract Due"},
    "auc.fine":            {"en": "⚠️ Fine"},
    "auc.mods":            {"en": "🔧 Mods (required / limited to)"},
    "auc.howto":           {"en": "Press “Bid Lower” to offer to do this mission for less. "
                                  "Lowest bid when the timer ends wins."},
    "auc.winner":          {"en": "🏆 Result"},
    "auc.won_for":         {"en": "**{name}** won for **{price}** {sym}"},
    "auc.no_winner":       {"en": "No bids were placed; escrow refunded to the issuer."},
    "auc.won_dm":          {"en": "🏆 You won the auction! Complete this mission for **{price}** {sym}."},
    # Buttons
    "auc.btn_bid":         {"en": "📉 Bid Lower"},
    "auc.btn_end":         {"en": "🛑 End now"},
    # Command feedback / errors
    "auc.err_disabled":    {"en": "❌ Auctions are not configured (no channel set in settings)."},
    "auc.err_amount":      {"en": "❌ Starting price must be a positive amount."},
    "auc.err_duration":    {"en": "❌ Duration must be between {min} and {max} hours."},
    "auc.err_date":        {"en": "❌ Invalid date. Use YYYY-MM-DD (must be in the future)."},
    "auc.err_funds":       {"en": "❌ Insufficient balance. You must escrow **{need}** {sym}."},
    "auc.err_limit":       {"en": "❌ Active contract limit reached ({max})."},
    "auc.err_post":        {"en": "❌ Could not post to the auction channel. Escrow refunded."},
    "auc.created":         {"en": "✅ Auction posted to {channel}."},
    # Bidding
    "auc.bid_modal_title": {"en": "Place a lower bid"},
    "auc.bid_field":       {"en": "Your price in KCoins"},
    "auc.bid_closed":      {"en": "❌ This auction has already ended."},
    "auc.bid_issuer":      {"en": "❌ You can't bid on your own auction."},
    "auc.bid_nan":         {"en": "❌ Enter a whole number."},
    "auc.bid_toohigh":     {"en": "❌ Bid must be at most **{max}** {sym}; undercut the current lowest by ≥ {step}."},
    "auc.bid_low":         {"en": "❌ Bid must be a positive amount."},
    "auc.bid_ok":          {"en": "✅ Bid placed: **{amount}** {sym}. You're the lowest bidder!"},
    # Ending
    "auc.end_issuer_only": {"en": "❌ Only the issuer can end this auction."},
    "auc.ended":           {"en": "✅ Auction ended."},
})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _epoch(iso: str) -> int:
    """ISO (naive UTC) → unix timestamp for Discord <t:…> markup."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def _auction_embed(a: dict, gid: int) -> discord.Embed:
    sym = settings.CURRENCY_SYMBOL
    status = a.get("status", adb.OPEN)
    if status == adb.OPEN:
        title, color = t(gid, "auc.title_open"), discord.Color.gold()
    elif status == adb.CLOSED:
        title, color = t(gid, "auc.title_closed"), discord.Color.green()
    else:
        title, color = t(gid, "auc.title_cancelled"), discord.Color.red()

    e = discord.Embed(title=title, color=color)
    e.add_field(name=t(gid, "auc.mission"), value=a["mission"], inline=False)
    e.add_field(name=t(gid, "auc.issuer"), value=a["issuer_name"], inline=True)
    e.add_field(name=t(gid, "auc.start"), value=f"**{a['start_value']}** {sym}", inline=True)

    if status == adb.OPEN:
        if a["bid_count"] > 0:
            cur = f"**{a['current_bid']}** {sym} · {t(gid, 'auc.bidder', name=a['current_bidder_name'])}"
        else:
            cur = t(gid, "auc.nobids")
        e.add_field(name=t(gid, "auc.current"), value=cur, inline=True)
        e.add_field(name=t(gid, "auc.bids"), value=str(a["bid_count"]), inline=True)
        e.add_field(name=t(gid, "auc.ends"), value=f"<t:{_epoch(a['ends_at'])}:R>", inline=True)
    elif status == adb.CLOSED:
        e.add_field(name=t(gid, "auc.winner"),
                    value=t(gid, "auc.won_for", name=a["current_bidder_name"],
                            price=a["current_bid"], sym=sym), inline=False)
    else:
        e.add_field(name=t(gid, "auc.winner"), value=t(gid, "auc.no_winner"), inline=False)

    e.add_field(name=t(gid, "auc.due"), value=a["due_date"], inline=True)
    e.add_field(name=t(gid, "auc.fine"), value=f"**{a['fine']}** {sym}", inline=True)
    if a.get("modlist"):
        mod_text = a["modlist"]
        if len(mod_text) > 1000:
            mod_text = mod_text[:1000] + "..."
        e.add_field(name=t(gid, "auc.mods"), value=f"```\n{mod_text}\n```", inline=False)
    if status == adb.OPEN:
        e.set_footer(text=t(gid, "auc.howto"))
    return e


async def _edit_auction_message(bot, a: dict, gid: int, view) -> None:
    """Re-render the public auction message (best-effort)."""
    if not a.get("channel_id") or not a.get("message_id"):
        return
    try:
        ch = bot.get_channel(int(a["channel_id"])) or await bot.fetch_channel(int(a["channel_id"]))
        msg = await ch.fetch_message(int(a["message_id"]))
        await msg.edit(embed=_auction_embed(a, gid), view=view)
    except Exception as exc:
        log.warning("Could not edit auction message %s: %s", a.get("auction_id"), exc)


async def open_auction(bot, gid: int, issuer_id: int, issuer_name: str, mission: str,
                       start_value: int, fine: int, due_date: str, duration_hours: int,
                       mods: str | None, mission_type: str | None = None) -> dict:
    """Escrow `start_value`, create the auction doc, and post it to the auction
    channel. Returns the auction doc. Raises (after refunding the escrow) if the
    post fails. Callers must pre-validate balance / limit / date / duration and
    that settings.AUCTION_CHANNEL_ID is set. Shared by the /auction slash command
    and the KSP-mod API endpoint.

    The escrow debit is atomic (try_debit): even though callers pre-validate the
    balance, that check and this debit aren't a single operation, so a concurrent
    request could otherwise escrow twice from the same funds. Raises ValueError on
    insufficient funds (caller surfaces it as the same 'insufficient balance')."""
    if not await store.try_debit(gid, issuer_id, start_value):
        raise ValueError("insufficient_balance")
    ends_at = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
    a = adb.create_auction(
        gid, issuer_id, issuer_name, mission, start_value, fine, due_date, ends_at,
        modlist=mods, min_decrement=settings.AUCTION_MIN_DECREMENT, mission_type=mission_type,
    )
    try:
        ch = (bot.get_channel(settings.AUCTION_CHANNEL_ID)
              or await bot.fetch_channel(settings.AUCTION_CHANNEL_ID))
        msg = await ch.send(embed=_auction_embed(a, gid),
                            view=AuctionLiveView(a["auction_id"], gid))
        adb.update_auction(gid, a["auction_id"], channel_id=str(ch.id), message_id=str(msg.id))
        a["channel_id"], a["message_id"] = str(ch.id), str(msg.id)
    except Exception:
        await store.add_balance(gid, issuer_id, start_value)  # refund escrow
        adb.update_auction(gid, a["auction_id"], status=adb.CANCELLED)
        raise
    return a


async def close_auction(bot, gid: int, auction_id: str, *, ended_by: str = "time") -> None:
    """Close an auction: bind the winner to an active contract (or refund if no bids).
    Idempotent — a second call after the status changed is a no-op."""
    a = adb.get_auction(gid, auction_id)
    if not a or a["status"] != adb.OPEN:
        return
    sym = settings.CURRENCY_SYMBOL
    winner_id = a.get("current_bidder_id")

    if winner_id:
        final = a["current_bid"]
        c = cdb.create_contract(
            gid, int(a["issuer_id"]), a["issuer_name"],
            int(winner_id), a["current_bidder_name"],
            a["mission"], final, a["fine"], a["due_date"],
            modlist=a.get("modlist"),
            mission_type=a.get("mission_type"),
        )
        cdb.update_contract(gid, c["contract_id"], status=cdb.ACTIVE)
        c["status"] = cdb.ACTIVE
        # Refund the part of the escrow above the winning bid.
        refund = a["start_value"] - final
        if refund > 0:
            await store.add_balance(gid, int(a["issuer_id"]), refund)
        a["status"] = adb.CLOSED
        adb.update_auction(gid, auction_id, status=adb.CLOSED, result_contract_id=c["contract_id"])
        log.info("Auction %s closed (%s) → contract %s, winner %s for %d",
                 auction_id, ended_by, c["contract_id"], a["current_bidder_name"], final)
        # DM the winner their active contract with the work view.
        try:
            winner = await bot.fetch_user(int(winner_id))
            e = _embed(c, gid)
            e.description = t(gid, "auc.won_dm", price=final, sym=sym)
            dm = await winner.send(embed=e, view=ContractWorkView(c["contract_id"], gid))
            cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm.id))
        except Exception as exc:
            log.warning("Could not DM auction winner %s: %s", winner_id, exc)
    else:
        # No bids — refund the full escrow and cancel.
        await store.add_balance(gid, int(a["issuer_id"]), a["start_value"])
        a["status"] = adb.CANCELLED
        adb.update_auction(gid, auction_id, status=adb.CANCELLED)
        log.info("Auction %s closed (%s) with no bids — escrow refunded", auction_id, ended_by)

    await _edit_auction_message(bot, a, gid, view=None)


# ── Buttons / Modal ──────────────────────────────────────────────────────────

_AID = r"(?P<aid>[^:]+):(?P<gid>\d+)"


def _acid(prefix: str, auction_id: str, guild_id: int) -> str:
    return f"{prefix}:{auction_id}:{guild_id}"


class BidModal(discord.ui.Modal):
    def __init__(self, auction_id: str, guild_id: int):
        super().__init__(title=t(guild_id, "auc.bid_modal_title"))
        self.aid = auction_id
        self.gid = guild_id
        self.amount = discord.ui.TextInput(
            label=t(guild_id, "auc.bid_field"),
            placeholder="e.g. 450",
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid, uid = self.gid, interaction.user.id
        sym = settings.CURRENCY_SYMBOL

        try:
            amount = int(self.amount.value.strip().replace(",", ""))
        except ValueError:
            await interaction.followup.send(tp(gid, uid, "auc.bid_nan"), ephemeral=True)
            return
        if amount <= 0:
            await interaction.followup.send(tp(gid, uid, "auc.bid_low"), ephemeral=True)
            return

        # Re-read fresh to validate against the latest lowest bid (mitigates races).
        a = adb.get_auction(gid, self.aid)
        if not a or a["status"] != adb.OPEN or a["ends_at"] <= datetime.utcnow().isoformat():
            await interaction.followup.send(tp(gid, uid, "auc.bid_closed"), ephemeral=True)
            return
        if str(uid) == str(a["issuer_id"]):
            await interaction.followup.send(tp(gid, uid, "auc.bid_issuer"), ephemeral=True)
            return

        step = a.get("min_decrement", 1)
        ceiling = a["current_bid"] - step
        if amount > ceiling:
            await interaction.followup.send(
                tp(gid, uid, "auc.bid_toohigh", max=ceiling, sym=sym, step=step), ephemeral=True)
            return

        fields = {
            "current_bid": amount,
            "current_bidder_id": str(uid),
            "current_bidder_name": interaction.user.display_name,
            "bid_count": a["bid_count"] + 1,
        }
        # Anti-snipe: a late bid pushes the end back so others can respond.
        if settings.AUCTION_ANTISNIPE_SECONDS > 0:
            now = datetime.utcnow()
            end_dt = datetime.fromisoformat(a["ends_at"])
            if (end_dt - now).total_seconds() < settings.AUCTION_ANTISNIPE_SECONDS:
                fields["ends_at"] = (now + timedelta(seconds=settings.AUCTION_ANTISNIPE_SECONDS)).isoformat()
        adb.update_auction(gid, self.aid, **fields)
        a.update(fields)

        await _edit_auction_message(interaction.client, a, gid, view=AuctionLiveView(self.aid, gid))
        await interaction.followup.send(
            tp(gid, uid, "auc.bid_ok", amount=amount, sym=sym), ephemeral=True)


class BidButton(DynamicItem[Button], template=r"auc_bid:" + _AID):
    def __init__(self, auction_id: str, guild_id: int):
        super().__init__(Button(label="📉 Bid Lower", style=discord.ButtonStyle.blurple,
                                custom_id=_acid("auc_bid", auction_id, guild_id)))
        self.aid = auction_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["aid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        gid, uid = self.gid, interaction.user.id
        a = adb.get_auction(gid, self.aid)
        if not a or a["status"] != adb.OPEN or a["ends_at"] <= datetime.utcnow().isoformat():
            await interaction.response.send_message(tp(gid, uid, "auc.bid_closed"), ephemeral=True)
            return
        if str(uid) == str(a["issuer_id"]):
            await interaction.response.send_message(tp(gid, uid, "auc.bid_issuer"), ephemeral=True)
            return
        await interaction.response.send_modal(BidModal(self.aid, gid))


class EndAuctionButton(DynamicItem[Button], template=r"auc_end:" + _AID):
    def __init__(self, auction_id: str, guild_id: int):
        super().__init__(Button(label="🛑 End now", style=discord.ButtonStyle.grey,
                                custom_id=_acid("auc_end", auction_id, guild_id)))
        self.aid = auction_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["aid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        gid, uid = self.gid, interaction.user.id
        a = adb.get_auction(gid, self.aid)
        if not a or a["status"] != adb.OPEN:
            await interaction.response.send_message(tp(gid, uid, "auc.bid_closed"), ephemeral=True)
            return
        if str(uid) != str(a["issuer_id"]):
            await interaction.response.send_message(tp(gid, uid, "auc.end_issuer_only"), ephemeral=True)
            return
        await interaction.response.defer()
        await close_auction(interaction.client, gid, self.aid, ended_by="issuer")
        await interaction.followup.send(tp(gid, uid, "auc.ended"), ephemeral=True)


class AuctionLiveView(View):
    def __init__(self, auction_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(BidButton(auction_id, guild_id))
        self.add_item(EndAuctionButton(auction_id, guild_id))


# ── Cog ──────────────────────────────────────────────────────────────────────

class Auctions(commands.Cog, name="Auctions"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.close_loop.start()

    async def cog_unload(self):
        self.close_loop.cancel()

    @app_commands.command(name="auction",
                          description="Post a reverse auction where contractors bid the price down")
    @app_commands.describe(
        mission="Mission description",
        start_value="Starting (maximum) payment in KCoins, escrowed up front",
        date_due="Contract due date once won (YYYY-MM-DD)",
        duration_hours="How many hours the auction runs",
        fine="Fine if the winner breaches the contract (default 0)",
        mods="Mods required / limited to (optional)",
    )
    async def auction(
        self, interaction: discord.Interaction,
        mission: str, start_value: int, date_due: str, duration_hours: int,
        fine: int = 0, mods: str | None = None,
    ):
        gid, uid = interaction.guild_id, interaction.user.id
        sym = settings.CURRENCY_SYMBOL

        if not settings.AUCTION_CHANNEL_ID:
            await interaction.response.send_message(tp(gid, uid, "auc.err_disabled"), ephemeral=True)
            return
        if start_value <= 0:
            await interaction.response.send_message(tp(gid, uid, "auc.err_amount"), ephemeral=True)
            return
        if fine < 0:
            fine = 0
        if not (settings.AUCTION_MIN_DURATION_HOURS <= duration_hours <= settings.AUCTION_MAX_DURATION_HOURS):
            await interaction.response.send_message(
                tp(gid, uid, "auc.err_duration",
                   min=settings.AUCTION_MIN_DURATION_HOURS, max=settings.AUCTION_MAX_DURATION_HOURS),
                ephemeral=True)
            return
        try:
            from datetime import date
            dt = datetime.strptime(date_due, "%Y-%m-%d").date()
            if dt <= date.today():
                raise ValueError
        except ValueError:
            await interaction.response.send_message(tp(gid, uid, "auc.err_date"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        bal = store.get_user(gid, uid)["balance"]
        if bal < start_value:
            await interaction.followup.send(
                tp(gid, uid, "auc.err_funds", need=start_value, sym=sym), ephemeral=True)
            return
        if cdb.count_active(gid, uid) >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
            await interaction.followup.send(
                tp(gid, uid, "auc.err_limit", max=settings.MAX_ACTIVE_CONTRACTS_PER_USER), ephemeral=True)
            return

        # Escrow + create + post (leftover escrow is refunded when the auction closes).
        try:
            a = await open_auction(self.bot, gid, uid, interaction.user.display_name,
                                   mission, start_value, fine, date_due, duration_hours, mods)
        except Exception as exc:
            log.error("Failed to post auction: %s", exc)
            await interaction.followup.send(tp(gid, uid, "auc.err_post"), ephemeral=True)
            return

        await interaction.followup.send(
            tp(gid, uid, "auc.created", channel=f"<#{a['channel_id']}>"), ephemeral=True)

    # ── Background: close auctions whose timer has elapsed ────────────────────
    @tasks.loop(seconds=30)
    async def close_loop(self):
        now = datetime.utcnow().isoformat()
        for guild in list(self.bot.guilds):
            try:
                for a in adb.list_open(guild.id):
                    if a.get("ends_at") and a["ends_at"] <= now:
                        await close_auction(self.bot, guild.id, a["auction_id"], ended_by="time")
            except Exception as exc:
                log.error("Auction close_loop error in guild %s: %s", guild.id, exc)

    @close_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Auctions(bot))
    # Persistent buttons — survive restarts via regex-matched custom_ids.
    bot.add_dynamic_items(BidButton, EndAuctionButton)
