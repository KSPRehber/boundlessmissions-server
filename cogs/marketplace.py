"""
cogs/marketplace.py – Craft marketplace.

Players list crafts for sale from the KSP mod (see api_server.py
/api/v1/marketplace/list). Each listing is posted to MARKETPLACE_CHANNEL_ID as an
embed with a Buy button. Clicking Buy transfers KCoins from buyer to seller (seller
gets the full price) and DMs the buyer the .craft blueprint. Listings are
non-exclusive — they stay active after a sale so anyone can buy a copy.

Buttons use DynamicItem with regex-matched custom_ids so they survive bot restarts.
custom_id format: "prefix:listing_id:guild_id"
"""
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, DynamicItem

import settings
from data.store import store
from data import marketplace as mkt
from data import imports as imp
from data.contracts import download_url

log = logging.getLogger(__name__)

# listing_ids are 12-char hex, guild_ids are snowflakes
_ID_PATTERN = r"(?P<lid>[^:]+):(?P<gid>\d+)"


def _cid(prefix: str, listing_id: str, guild_id: int) -> str:
    return f"{prefix}:{listing_id}:{guild_id}"


def listing_embed(listing: dict) -> discord.Embed:
    """Build the marketplace channel embed for a listing."""
    sym = settings.CURRENCY_SYMBOL
    delisted = listing.get("status") != mkt.ACTIVE
    e = discord.Embed(
        title=f"🛒 {listing['craft_name']}",
        description=("~~This craft is no longer for sale.~~" if delisted
                     else "Click **Buy** to purchase this craft. The blueprint will be sent to your DMs."),
        color=discord.Color.dark_grey() if delisted else discord.Color.blurple(),
    )
    e.add_field(name="Price", value=f"**{listing['price']:,}** {sym}", inline=True)
    e.add_field(name="Editor", value=listing.get("craft_type", "—"), inline=True)
    e.add_field(name="Parts", value=f"{listing.get('part_count', 0)}", inline=True)
    e.add_field(name="Mass", value=f"{listing.get('mass', 0):.1f} t", inline=True)
    e.add_field(name="Cost", value=f"{listing.get('cost', 0):,.0f}", inline=True)
    e.add_field(name="Seller", value=listing.get("seller_name", "—"), inline=True)
    # The rendered blueprint is always shown publicly; the .craft file itself is
    # only delivered to the buyer's DMs after purchase.
    if listing.get("blueprint_url"):
        e.set_image(url=listing["blueprint_url"])
    e.set_footer(text=f"Listing {listing['listing_id']} · {listing.get('sales_count', 0)} sold")
    return e


# ══════════════════════════════════════════════════════════════════════════════
#  DynamicItem Buttons
# ══════════════════════════════════════════════════════════════════════════════

class BuyButton(DynamicItem[Button], template=r"mk_buy:" + _ID_PATTERN):
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🛒 Buy", style=discord.ButtonStyle.green,
                                custom_id=_cid("mk_buy", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid, lid = self.gid, self.lid
        buyer_id = interaction.user.id

        listing = mkt.get_listing(gid, lid)
        if not listing or listing.get("status") != mkt.ACTIVE:
            await interaction.followup.send("❌ This craft is no longer for sale.", ephemeral=True)
            return

        seller_id = int(listing["seller_id"])
        if buyer_id == seller_id:
            await interaction.followup.send("❌ You can't buy your own listing.", ephemeral=True)
            return

        price = int(listing["price"])
        already_owned = str(buyer_id) in listing.get("buyers", [])

        # Charge only first-time buyers; repeat buyers get a free re-delivery.
        if not already_owned:
            buyer = store.get_user(gid, buyer_id)
            if buyer["balance"] < price:
                await interaction.followup.send(
                    f"❌ You need **{price:,}** {settings.CURRENCY_SYMBOL} but only have "
                    f"**{buyer['balance']:,}**.",
                    ephemeral=True,
                )
                return
            await store.add_balance(gid, buyer_id, -price)
            await store.add_balance(gid, seller_id, price)

        # Download the blueprint and DM it to the buyer.
        try:
            data = await download_url(listing["craft_url"])
        except Exception as exc:
            log.error("Failed to download craft for listing %s: %s", lid, exc)
            if not already_owned:  # refund — we charged but can't deliver
                await store.add_balance(gid, buyer_id, price)
                await store.add_balance(gid, seller_id, -price)
            await interaction.followup.send("❌ Could not fetch the craft file. You were not charged.", ephemeral=True)
            return

        craft_file = discord.File(io.BytesIO(data), filename=listing["craft_filename"])
        try:
            await interaction.user.send(
                content=(f"🛒 Here is **{listing['craft_name']}** that you purchased.\n"
                         f"Place the `.craft` file in your KSP `Ships/{listing.get('craft_type', 'VAB')}/` "
                         f"folder, or hit **Load to KSP** to auto-import it at the Space Center."),
                file=craft_file,
                view=DMImportView(lid, gid),
            )
        except discord.Forbidden:
            if not already_owned:  # refund — DMs closed, no delivery
                await store.add_balance(gid, buyer_id, price)
                await store.add_balance(gid, seller_id, -price)
            await interaction.followup.send(
                "❌ I couldn't DM you. Enable **Direct Messages** from server members and try again. "
                "You were not charged.",
                ephemeral=True,
            )
            return

        if already_owned:
            await interaction.followup.send("✅ Re-sent the blueprint to your DMs (free — you already own it).", ephemeral=True)
            return

        # Record the sale and refresh the channel embed.
        mkt.record_purchase(gid, lid, buyer_id)
        listing = mkt.get_listing(gid, lid)
        try:
            await interaction.message.edit(embed=listing_embed(listing), view=self.view)
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ Purchased **{listing['craft_name']}** for **{price:,}** {settings.CURRENCY_SYMBOL}. "
            "Check your DMs for the blueprint!",
            ephemeral=True,
        )

        # Notify the seller.
        try:
            seller = await interaction.client.fetch_user(seller_id)
            await seller.send(
                f"💰 **{interaction.user.display_name}** bought your craft **{listing['craft_name']}** "
                f"for **{price:,}** {settings.CURRENCY_SYMBOL}."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        log.info("Marketplace: %s bought listing %s for %d", interaction.user, lid, price)


class LoadToKspButton(DynamicItem[Button], template=r"mk_load:" + _ID_PATTERN):
    """Sent in the purchase DM — queues the bought craft for KSP auto-import."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🚀 Load to KSP", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("mk_load", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = mkt.get_listing(self.gid, self.lid)
        if not listing:
            await interaction.followup.send("❌ This craft is no longer available.", ephemeral=True)
            return
        imp.enqueue(
            self.gid, interaction.user.id, "market", self.lid, listing["craft_name"],
            craft_url=listing["craft_url"], craft_filename=listing["craft_filename"],
        )
        await interaction.followup.send(
            "✅ Queued! Open KSP and it'll auto-import at the Space Center.", ephemeral=True)
        log.info("Marketplace: %s queued listing %s for KSP import", interaction.user, self.lid)


class DMImportView(View):
    """Attached to the purchase DM so the buyer can one-click load into KSP."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(LoadToKspButton(listing_id, guild_id))


class DelistButton(DynamicItem[Button], template=r"mk_delist:" + _ID_PATTERN):
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🗑️ Delist", style=discord.ButtonStyle.grey,
                                custom_id=_cid("mk_delist", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = mkt.get_listing(self.gid, self.lid)
        if not listing:
            await interaction.followup.send("❌ Listing not found.", ephemeral=True)
            return

        if interaction.user.id != int(listing["seller_id"]):
            await interaction.followup.send("❌ Only the seller can delist this craft.", ephemeral=True)
            return

        if listing.get("status") == mkt.ACTIVE:
            mkt.update_listing(self.gid, self.lid, status=mkt.DELISTED)
            listing["status"] = mkt.DELISTED
        try:
            await interaction.message.edit(embed=listing_embed(listing), view=None)
        except Exception:
            pass
        await interaction.followup.send("✅ Craft delisted.", ephemeral=True)
        log.info("Marketplace: %s delisted listing %s", interaction.user, self.lid)


class ListingView(View):
    """Persistent view attached to each listing message."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(BuyButton(listing_id, guild_id))
        self.add_item(DelistButton(listing_id, guild_id))


MARKETPLACE_DYNAMIC_ITEMS = [BuyButton, DelistButton, LoadToKspButton]


async def post_listing(bot: commands.Bot, guild_id: int, listing: dict) -> int | None:
    """Post a listing embed to the marketplace channel. Returns the message id."""
    if not settings.MARKETPLACE_CHANNEL_ID:
        log.warning("MARKETPLACE_CHANNEL_ID not set — cannot post listing %s", listing["listing_id"])
        return None
    channel = bot.get_channel(settings.MARKETPLACE_CHANNEL_ID)
    if channel is None:
        log.warning("Marketplace channel %s not found", settings.MARKETPLACE_CHANNEL_ID)
        return None
    view = ListingView(listing["listing_id"], guild_id)
    msg = await channel.send(embed=listing_embed(listing), view=view)
    mkt.update_listing(guild_id, listing["listing_id"], channel_msg_id=str(msg.id))
    return msg.id


# ══════════════════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════════════════

class Marketplace(commands.Cog, name="Marketplace"):
    """Craft marketplace commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="market", description="Browse crafts for sale")
    async def market(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        listings = sorted(mkt.list_active(gid), key=lambda l: l.get("created_at", ""), reverse=True)
        if not listings:
            await interaction.response.send_message("🛒 No crafts are for sale right now.", ephemeral=True)
            return

        sym = settings.CURRENCY_SYMBOL
        lines = [
            f"**{l['craft_name']}** — {l['price']:,} {sym} · {l.get('part_count', 0)} parts "
            f"· by {l.get('seller_name', '—')} · `{l['listing_id']}`"
            for l in listings[:25]
        ]
        embed = discord.Embed(
            title="🛒 Craft Marketplace",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        if settings.MARKETPLACE_CHANNEL_ID:
            embed.set_footer(text="Buy crafts from the marketplace channel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delist", description="Remove one of your craft listings")
    @app_commands.describe(listing_id="The listing ID (shown on the listing and in /market)")
    async def delist(self, interaction: discord.Interaction, listing_id: str) -> None:
        gid = interaction.guild_id
        listing = mkt.get_listing(gid, listing_id)
        if not listing:
            await interaction.response.send_message("❌ Listing not found.", ephemeral=True)
            return
        if interaction.user.id != int(listing["seller_id"]):
            await interaction.response.send_message("❌ Only the seller can delist this craft.", ephemeral=True)
            return

        if listing.get("status") == mkt.ACTIVE:
            mkt.update_listing(gid, listing_id, status=mkt.DELISTED)
            listing["status"] = mkt.DELISTED

        # Update the channel message if we know it.
        msg_id = listing.get("channel_msg_id")
        if msg_id and settings.MARKETPLACE_CHANNEL_ID:
            channel = self.bot.get_channel(settings.MARKETPLACE_CHANNEL_ID)
            if channel:
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.edit(embed=listing_embed(listing), view=None)
                except (discord.NotFound, discord.HTTPException):
                    pass
        await interaction.response.send_message("✅ Craft delisted.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Marketplace(bot))
    bot.add_dynamic_items(*MARKETPLACE_DYNAMIC_ITEMS)
