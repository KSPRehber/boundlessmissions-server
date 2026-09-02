"""
cogs/auctions.py – Reverse (Dutch) auctions for contracts.

An issuer posts a mission with a STARTING price (escrowed up front). Contractors
bid the price DOWN via a modal; the lowest bid when the auction ends wins and is
bound to an ACTIVE contract for that amount. Leftover escrow is refunded.

Auctions end either when their timer elapses (a background loop closes them) or
when the issuer presses "End now". Bid/End buttons are DynamicItems, so they keep
working across restarts; the loop makes timed closes restart-safe too.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, DynamicItem

import settings
from data.store import store
from data import accounts
from data import auctions as adb
from data import contracts as cdb
from data import guild_config
from i18n import t, tp, S
from cogs.contract_views import ContractWorkView, _embed, _fit_field
import contract_actions as ca

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "auc.title_open":      {"en": "🔨 Reverse Auction"},
    "auc.title_closed":    {"en": "🔨 Auction Closed"},
    "auc.title_cancelled": {"en": "🔨 Auction Cancelled"},
    "auc.title_flag_open": {"en": "🚩 Flag Design Auction"},
    "auc.mission":         {"en": "📋 Mission"},
    "auc.work":            {"en": "🛠️ Work"},
    "auc.work_craft":      {"en": "Craft build: submit a blueprint from the VAB/SPH"},
    "auc.work_active":     {"en": "Active mission: fly a craft to the target"},
    "auc.work_flag":       {"en": "Flag design: submitted and reviewed here in Discord"},
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
    # Bidding
    "auc.bid_modal_title": {"en": "Place a lower bid"},
    "auc.bid_field":       {"en": "Your price in KCoins"},
    "auc.bid_closed":      {"en": "❌ This auction has already ended."},
    "auc.bid_issuer":      {"en": "❌ You can't bid on your own auction."},
    "auc.bid_nan":         {"en": "❌ Enter a whole number."},
    "auc.bid_toohigh":     {"en": "❌ Bid must be at most **{max}** {sym}; undercut the current lowest by ≥ {step}."},
    "auc.bid_low":         {"en": "❌ Bid must be a positive amount."},
    "auc.bid_ok":          {"en": "✅ Bid placed: **{amount}** {sym}. You're the lowest bidder!"},
    "auc.bid_fine_cap":    {"en": "❌ Bids below **{floor}** {sym} would carry a fine over {mult}× "
                                  "the payment (this auction's fine is {fine} {sym})."},
    "auc.bid_gate":        {"en": "❌ {reason}"},
    "auc.winner_refused":  {"en": "The winning bidder could not take the contract ({reason}); "
                                  "escrow refunded to the issuer."},
    # Ending
    "auc.end_issuer_only": {"en": "❌ Only the issuer can end this auction."},
    "auc.ended":           {"en": "✅ Auction ended."},
})


# The work types an auction can pin for the winner's contract, and the line that
# describes each on the auction card. Same set the KSP mod and the browser UI offer
# (api_server's /auctions/create allow-list); a rescue is never auctioned, because
# issuing one destroys the issuer's vessel for a contractor who is not known yet.
_WORK_KEYS = {
    "craft_build": "auc.work_craft",
    "active_vessel": "auc.work_active",
    cdb.FLAG_DESIGN: "auc.work_flag",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _epoch(iso: str) -> int:
    """ISO (naive UTC) → unix timestamp for Discord <t:…> markup."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def _work_line(a: dict, gid: int) -> str | None:
    """What the winner will actually have to do, when the auction pins it. Untyped
    auctions (the default) say nothing — there is nothing to say."""
    key = _WORK_KEYS.get(a.get("mission_type"))
    return t(gid, key) if key else None


def _auction_embed(a: dict, gid: int) -> discord.Embed:
    sym = settings.CURRENCY_SYMBOL
    status = a.get("status", adb.OPEN)
    is_flag = a.get("mission_type") == cdb.FLAG_DESIGN
    if status == adb.OPEN:
        title = t(gid, "auc.title_flag_open") if is_flag else t(gid, "auc.title_open")
        color = discord.Color.gold()
    elif status == adb.CLOSED:
        title, color = t(gid, "auc.title_closed"), discord.Color.green()
    else:
        title, color = t(gid, "auc.title_cancelled"), discord.Color.red()

    e = discord.Embed(title=title, color=color)
    # The mission text and both display names are written by players, and an embed
    # field renders full markdown — including masked links. This card is worse than
    # the contract one it mirrors: `open_auction` posts it into EVERY guild the bot
    # serves that has an auction channel, and `_edit_auction_message` re-renders all
    # of those mirrors on every bid — and a bid escrows nothing, so the vector is
    # free to re-trigger. A nickname of `[Verify account](https://evil.tld)` fits in
    # 25 of the 32 characters a display name allows.
    #
    # Escaped at DISPLAY only: the same three strings are handed to
    # `cdb.create_contract` when the auction closes, and the stored record must stay
    # the text the player actually wrote. Fit-then-escape, for the reason
    # `contract_views._embed` documents — escaping first spends the field budget on
    # backslashes and lets truncation cut a `\*` pair in half.
    _esc = discord.utils.escape_markdown
    e.add_field(name=t(gid, "auc.mission"),
                value=_esc(_fit_field(str(a["mission"] or ""))), inline=False)
    # Bidders price the job, so the kind of job has to be on the card — a flag design
    # is not bid the way a craft build is, and it is submitted somewhere else.
    work = _work_line(a, gid)
    if work:
        e.add_field(name=t(gid, "auc.work"), value=work, inline=False)
    e.add_field(name=t(gid, "auc.issuer"), value=_esc(str(a["issuer_name"] or "")), inline=True)
    e.add_field(name=t(gid, "auc.start"), value=f"**{a['start_value']}** {sym}", inline=True)

    if status == adb.OPEN:
        if a["bid_count"] > 0:
            cur = (f"**{a['current_bid']}** {sym} · "
                   + t(gid, 'auc.bidder', name=_esc(str(a['current_bidder_name'] or ''))))
        else:
            cur = t(gid, "auc.nobids")
        e.add_field(name=t(gid, "auc.current"), value=cur, inline=True)
        e.add_field(name=t(gid, "auc.bids"), value=str(a["bid_count"]), inline=True)
        e.add_field(name=t(gid, "auc.ends"), value=f"<t:{_epoch(a['ends_at'])}:R>", inline=True)
    elif status == adb.CLOSED:
        e.add_field(name=t(gid, "auc.winner"),
                    value=t(gid, "auc.won_for",
                            name=_esc(str(a["current_bidder_name"] or "")),
                            price=a["current_bid"], sym=sym), inline=False)
    elif a.get("closed_reason"):
        # Ended with a lowest bidder who could not be bound (see close): saying
        # "no bids were placed" over a card that showed bids reads as a bug.
        e.add_field(name=t(gid, "auc.winner"),
                    value=t(gid, "auc.winner_refused", reason=a["closed_reason"]),
                    inline=False)
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


async def _edit_auction_message(bot, a: dict, gid: int, *, live: bool) -> None:
    """Re-render EVERY mirrored copy of the auction message (best-effort). When
    `live` the Bid/End buttons are reattached; otherwise the message is read-only."""
    aid = a.get("auction_id", "")
    for m in a.get("mirrors", []) or []:
        try:
            ch = bot.get_channel(int(m["channel_id"])) or await bot.fetch_channel(int(m["channel_id"]))
            msg = await ch.fetch_message(int(m["message_id"]))
            view = AuctionLiveView(aid, int(m["guild_id"])) if live else None
            await msg.edit(embed=_auction_embed(a, gid), view=view)
        except Exception as exc:
            log.warning("Could not edit auction mirror %s in guild %s: %s",
                        aid, m.get("guild_id"), exc)


async def backfill_guild(bot, guild_id: int) -> int:
    """Mirror every still-open auction into `guild_id`'s auction channel — used when
    a server configures its auction channel after auctions already exist. Idempotent:
    skips auctions already mirrored in this guild. Returns count posted."""
    channel = guild_config.resolve_channel(bot, guild_id, "auction")
    if channel is None:
        return 0
    now = datetime.utcnow().isoformat()
    posted = 0
    for a in await asyncio.to_thread(adb.list_open, 0):
        if a.get("ends_at") and a["ends_at"] <= now:
            continue  # about to be closed by the loop — don't mirror a dead auction
        aid = a["auction_id"]
        mirrors = a.get("mirrors", []) or []
        if any(str(m.get("guild_id")) == str(guild_id) for m in mirrors):
            continue
        try:
            msg = await channel.send(
                embed=_auction_embed(a, int(a.get("guild_id") or guild_id)),
                view=AuctionLiveView(aid, guild_id))
        except Exception as exc:
            log.warning("Backfill: could not mirror auction %s into guild %s: %s", aid, guild_id, exc)
            continue
        mirrors.append({"guild_id": str(guild_id), "channel_id": str(channel.id),
                        "message_id": str(msg.id)})
        adb.update_auction(0, aid, mirrors=mirrors)
        posted += 1
    if posted:
        log.info("Backfilled %d open auctions into guild %s", posted, guild_id)
    return posted


async def open_auction(bot, gid: int, issuer_id: int, issuer_name: str, mission: str,
                       start_value: int, fine: int, due_date: str, duration_hours: int,
                       mods: str | None, mission_type: str | None = None) -> dict:
    """Escrow `start_value`, create the auction doc, and post it to the auction
    channel. Returns the auction doc. Raises (after refunding the escrow) if the
    post fails. Callers must pre-validate balance / limit / date / duration and
    that an auction channel is configured for this guild (guild_config). Called
    only by the KSP-mod API endpoint — auctions are opened from the game.

    The escrow debit is atomic (try_debit): even though callers pre-validate the
    balance, that check and this debit aren't a single operation, so a concurrent
    request could otherwise escrow twice from the same funds. Raises ValueError on
    insufficient funds (caller surfaces it as the same 'insufficient balance')."""
    if not await store.try_debit(gid, issuer_id, start_value,
                                 category=store.TX_AUCTION_ESCROW,
                                 detail=store.tx_detail(mission, "Auction opened")):
        raise ValueError("insufficient_balance")
    ends_at = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
    a = adb.create_auction(
        gid, issuer_id, issuer_name, mission, start_value, fine, due_date, ends_at,
        modlist=mods, min_decrement=settings.AUCTION_MIN_DECREMENT, mission_type=mission_type,
    )
    try:
        aid = a["auction_id"]
        mirrors: list[dict] = []
        for guild in bot.guilds:
            ch = guild_config.resolve_channel(bot, guild.id, "auction")
            if ch is None:
                continue
            try:
                msg = await ch.send(embed=_auction_embed(a, gid),
                                    view=AuctionLiveView(aid, guild.id))
                mirrors.append({"guild_id": str(guild.id), "channel_id": str(ch.id),
                                "message_id": str(msg.id)})
            except Exception as exc:
                log.warning("Could not mirror auction %s into guild %s: %s", aid, guild.id, exc)
        if not mirrors:
            raise RuntimeError("no auction channel configured in any server")
        adb.update_auction(gid, aid, mirrors=mirrors)
        a["mirrors"] = mirrors
    except Exception:
        # Through the same helper as every other escrow return, so there is exactly one
        # place that decides what happens when an issuer no longer has a record. Here
        # they certainly do (their escrow was debited moments ago in this same request)
        # — the point is that a fifth refund path added later inherits the guard.
        await _refund_issuer(gid, issuer_id, start_value,
                             detail="Auction could not be posted")
        adb.update_auction(gid, a["auction_id"], status=adb.CANCELLED)
        raise
    return a


def bid_refusal(gid: int, bidder_id) -> str | None:
    """Why `bidder_id` may not bid at all, or None.

    Winning binds the bidder to an ACTIVE contract exactly as `ca.accept` does, so
    the gates `accept` applies — the debt cap and the active-contract cap — apply
    to a bid. Checked here, on the event loop, rather than inside `try_place_bid`:
    that runs in a thread, and `store` is only ever read on the loop. `close_auction`
    re-checks, since both numbers can move between the bid and the close.
    """
    return ca.contractor_gate(gid, bidder_id)


_close_locks: dict[str, asyncio.Lock] = {}


async def close_auction(bot, gid: int, auction_id: str, *, ended_by: str = "time") -> None:
    """Close an auction: bind the winner to an active contract (or refund if no bids).
    Idempotent — a second call after the status changed is a no-op.

    Serialised per auction: the timer, the issuer's "End now" (Discord and web)
    and a mirror button can all arrive together, and the status write below
    lands *after* an awaited refund — see `contract_actions.serialized` for why
    that is not safe to leave to luck."""
    lock = _close_locks.get(auction_id)
    if lock is None:
        lock = _close_locks[auction_id] = asyncio.Lock()
    async with lock:
        try:
            await _close_auction_locked(bot, gid, auction_id, ended_by=ended_by)
        finally:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                _close_locks.pop(auction_id, None)


async def _refund_issuer(gid: int, issuer_id: str, amount: int, *, detail: str,
                         counterparty: str = "") -> None:
    """Return escrow to an auction's issuer, unless that account no longer exists.

    `store.add_balance` calls `get_user`, which MINTS a default record for an unknown
    id and marks it dirty — so refunding an issuer who ran delete-my-data recreates a
    `users/{id}` document holding their balance, which the next auto-save writes back
    to Firestore. The account asked to be erased and the erasure quietly undoes itself.
    It also has a second-order cost: `_garnish_locked` forgives debts owed to accounts
    with no record, so once the ghost exists a debtor is garnished forever into a wallet
    nobody can spend.

    `contract_actions._pay_issuer` already guards its refund this way; the three auction
    refunds did not, and an auction outlives its issuer's deletion because
    `/deletemydata` deliberately does not cancel auctions (they involve other players).
    """
    if amount <= 0:
        return
    if not store.has_user(str(issuer_id)):
        log.info("Auction refund: issuer %s has no account record; %d not credited",
                 issuer_id, amount)
        return
    await store.add_balance(gid, str(issuer_id), amount,
                            category=store.TX_AUCTION_REFUND,
                            detail=detail, counterparty=counterparty)


async def _close_auction_locked(bot, gid: int, auction_id: str, *, ended_by: str) -> None:
    # Claim the auction transactionally instead of reading it. This both freezes the
    # winner and the winning bid against a bid landing during an early close, and takes
    # the auction out of OPEN before any money moves so the 30-second sweeper cannot
    # close it twice. See `adb.claim_close`. Everything below MUST use the frozen `a`
    # and never re-read the document.
    a = adb.claim_close(auction_id)
    if not a:
        return
    # The winner's contract inherits the auction's ORIGIN guild (for channel routing
    # like the "sue" escalation), regardless of which server's mirror triggered close.
    origin_gid = int(a.get("guild_id") or gid)
    sym = settings.CURRENCY_SYMBOL
    winner_id = a.get("current_bidder_id")

    if winner_id:
        final = a["current_bid"]
        # The bid was gated (`bid_refusal`), but the debt total and the active
        # count both move between a bid and the close, and an auction opened
        # before the gate existed was never checked at all. A winner who cannot
        # be bound is not bound: the auction ends with the whole escrow back to
        # the issuer, both parties told why, and no contract — a debtor over the
        # cap must not become the contractor of an ACTIVE contract through the one
        # path where nobody had to accept anything.
        refusal = ca.contractor_gate(origin_gid, str(winner_id))
        if refusal:
            # Status first, money second: the claim left this CLOSED, so settle the
            # real terminal status before the refund. A failure after this point is a
            # cancelled auction whose escrow needs returning by hand — visible — rather
            # than a refund the next sweeper tick pays again.
            a["status"] = adb.CANCELLED
            a["closed_reason"] = refusal
            adb.update_auction(origin_gid, auction_id, status=adb.CANCELLED,
                               closed_reason=refusal)
            await _refund_issuer(origin_gid, a["issuer_id"], a["start_value"],
                                 detail="Auction winner could not take the contract",
                                 counterparty=str(winner_id))
            log.info("Auction %s closed (%s) but winner %s was refused: %s; escrow refunded",
                     auction_id, ended_by, winner_id, refusal)
            for who, text in (
                (str(winner_id),
                 f"🔨 You had the lowest bid on \"{a['mission'][:80]}\" but could not take "
                 f"the contract: {refusal}"),
                (str(a["issuer_id"]),
                 f"🔨 The lowest bidder on \"{a['mission'][:80]}\" could not take the "
                 f"contract ({refusal}). Your {a['start_value']} {sym} escrow was refunded."),
            ):
                try:
                    await ca.deliver_to_player(origin_gid, who, content=text)
                except Exception as exc:
                    log.warning("Could not notify %s about auction %s: %s", who, auction_id, exc)
            await _edit_auction_message(bot, a, origin_gid, live=False)
            return

        # The fine was capped against the START value when the auction opened, but
        # the payment is the winning bid. `try_place_bid` now refuses a bid that
        # would breach MAX_FINE_MULTIPLE_OF_PAYMENT; this clamps the contract for a
        # bid placed before that check existed, so the cap holds on every contract
        # regardless of when its auction was opened.
        fine = int(a.get("fine", 0) or 0)
        mult = settings.MAX_FINE_MULTIPLE_OF_PAYMENT
        if mult > 0 and fine > final * mult:
            log.info("Auction %s: fine %d capped to %d (%d× the %d winning bid)",
                     auction_id, fine, final * mult, mult, final)
            fine = final * mult

        # Ids are account ids and travel as the strings they are stored as —
        # create_contract/add_balance str() them anyway, and an int() here on a
        # web-origin id raised before the status write, leaving the auction OPEN
        # forever with the issuer's escrow inside it.
        c = cdb.create_contract(
            origin_gid, str(a["issuer_id"]), a["issuer_name"],
            str(winner_id), a["current_bidder_name"],
            a["mission"], final, fine, a["due_date"],
            modlist=a.get("modlist"),
            mission_type=a.get("mission_type"),
        )
        cdb.update_contract(origin_gid, c["contract_id"], status=cdb.ACTIVE)
        c["status"] = cdb.ACTIVE
        # Record the result BEFORE refunding, for the reason above: the claim already
        # wrote CLOSED, and this is what makes that status mean "settled" rather than
        # "claimed". Then refund the part of the escrow above the winning bid.
        a["status"] = adb.CLOSED
        adb.update_auction(origin_gid, auction_id, status=adb.CLOSED,
                           result_contract_id=c["contract_id"])
        refund = a["start_value"] - final
        if refund > 0:
            await _refund_issuer(origin_gid, a["issuer_id"], refund,
                                 detail="Escrow above the winning bid",
                                 counterparty=str(winner_id or ""))
        log.info("Auction %s closed (%s) → contract %s, winner %s for %d",
                 auction_id, ended_by, c["contract_id"], a["current_bidder_name"], final)
        # Hand the winner their active contract with the work view — corp channel
        # first, DM fallback (an auction winner may have no corp).
        try:
            e = _embed(c, origin_gid)
            e.description = t(origin_gid, "auc.won_dm", price=final, sym=sym)
            dm = await ca.deliver_to_player(
                origin_gid, str(winner_id), embed=e,
                view=ContractWorkView(c["contract_id"], origin_gid, c.get("mission_type")))
            if dm is not None:
                cdb.update_contract(origin_gid, c["contract_id"], dm_message_id=str(dm.id))
        except Exception as exc:
            log.warning("Could not notify auction winner %s: %s", winner_id, exc)
    else:
        # No bids — cancel, then refund the full escrow (status first, money second).
        a["status"] = adb.CANCELLED
        adb.update_auction(origin_gid, auction_id, status=adb.CANCELLED)
        await _refund_issuer(origin_gid, a["issuer_id"], a["start_value"],
                             detail="Auction closed with no bids")
        log.info("Auction %s closed (%s) with no bids, escrow refunded", auction_id, ended_by)

    await _edit_auction_message(bot, a, origin_gid, live=False)


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
        gid = self.gid
        # The ACCOUNT id, not the raw snowflake. The auction document stores
        # `issuer_id` as an account id, so the "you cannot bid on your own auction"
        # and "you cannot be your own contractor" guards — here and inside
        # `try_place_bid` — compared a snowflake to an `a_…` id and failed OPEN for
        # any player who linked Discord onto an existing account. They could bid on
        # their own auction and be bound to their own contract.
        _acct = accounts.account_for_discord(interaction.user.id)
        uid = _acct if _acct is not None else interaction.user.id
        sym = settings.CURRENCY_SYMBOL

        try:
            amount = int(self.amount.value.strip().replace(",", ""))
        except ValueError:
            await interaction.followup.send(tp(gid, uid, "auc.bid_nan"), ephemeral=True)
            return
        if amount <= 0:
            await interaction.followup.send(tp(gid, uid, "auc.bid_low"), ephemeral=True)
            return

        if refusal := bid_refusal(gid, uid):
            await interaction.followup.send(tp(gid, uid, "auc.bid_gate", reason=refusal),
                                            ephemeral=True)
            return

        # Validated and written inside one Firestore transaction (the same
        # `try_place_bid` the website uses): two bids landing together used to
        # both pass the ceiling against the same `current_bid`, and the later
        # write won even when it was the worse bid.
        res = await asyncio.to_thread(
            adb.try_place_bid, gid, self.aid, uid, interaction.user.display_name,
            amount, settings.AUCTION_ANTISNIPE_SECONDS)
        if not res["ok"]:
            reason = res["reason"]
            if reason == "own":
                await interaction.followup.send(tp(gid, uid, "auc.bid_issuer"), ephemeral=True)
            elif reason == "too_high":
                await interaction.followup.send(
                    tp(gid, uid, "auc.bid_toohigh", max=res["ceiling"], sym=sym,
                       step=res["step"]), ephemeral=True)
            elif reason == "fine_cap":
                await interaction.followup.send(
                    tp(gid, uid, "auc.bid_fine_cap", floor=res["floor"], fine=res["fine"],
                       mult=res["mult"], sym=sym), ephemeral=True)
            else:  # missing / closed / no_discord
                await interaction.followup.send(tp(gid, uid, "auc.bid_closed"), ephemeral=True)
            return
        a = res["auction"]

        await _edit_auction_message(interaction.client, a, int(a.get("guild_id") or gid), live=True)
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
        gid = self.gid
        # The ACCOUNT id, not the raw snowflake. The auction document stores
        # `issuer_id` as an account id, so the "you cannot bid on your own auction"
        # and "you cannot be your own contractor" guards — here and inside
        # `try_place_bid` — compared a snowflake to an `a_…` id and failed OPEN for
        # any player who linked Discord onto an existing account. They could bid on
        # their own auction and be bound to their own contract.
        _acct = accounts.account_for_discord(interaction.user.id)
        uid = _acct if _acct is not None else interaction.user.id
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
        gid = self.gid
        # The ACCOUNT id, not the raw snowflake. The auction document stores
        # `issuer_id` as an account id, so the "you cannot bid on your own auction"
        # and "you cannot be your own contractor" guards — here and inside
        # `try_place_bid` — compared a snowflake to an `a_…` id and failed OPEN for
        # any player who linked Discord onto an existing account. They could bid on
        # their own auction and be bound to their own contract.
        _acct = accounts.account_for_discord(interaction.user.id)
        uid = _acct if _acct is not None else interaction.user.id
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

    # No /auction slash command on purpose: auctions are OPENED from the KSP mod
    # only. Discord (and the website) is where they are mirrored, bid on and
    # closed — not where they start.

    # ── Background: close auctions whose timer has elapsed ────────────────────
    @tasks.loop(seconds=30)
    async def close_loop(self):
        now = datetime.utcnow().isoformat()
        try:
            # Auctions are global now — iterate the global open set once.
            # to_thread, not a bare call: this is a 30-second timer and list_open is
            # a blocking Firestore scan. On the event loop it stalls the gateway
            # heartbeat for as long as the query takes.
            for a in await asyncio.to_thread(adb.list_open, 0):
                if a.get("ends_at") and a["ends_at"] <= now:
                    await close_auction(self.bot, int(a.get("guild_id") or 0),
                                        a["auction_id"], ended_by="time")
        except Exception as exc:
            log.error("Auction close_loop error: %s", exc)

    @close_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Auctions(bot))
    # Persistent buttons — survive restarts via regex-matched custom_ids.
    bot.add_dynamic_items(BidButton, EndAuctionButton)
