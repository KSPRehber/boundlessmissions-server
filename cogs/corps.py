"""
cogs/corps.py – Corporation system.

Users can establish a corporation which creates a dedicated text channel.
Each user may own one corporation at a time. Data is persisted in Firestore.

Firestore path: guilds/{guild_id}/corps/{user_id}
"""

import logging
import datetime
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# Import the shared Firestore client
from data.store import _db
import settings
from i18n import t, tp
from cogs.gkchannels import add_gk_channel, remove_gk_channel


def _get_corp_ref(guild_id: int, user_id: int):
    """Get a Firestore document reference for a user's corporation."""
    return (
        _db.collection("guilds")
        .document(str(guild_id))
        .collection("corps")
        .document(str(user_id))
    )


async def _create_corp_channel(
    guild: discord.Guild,
    owner: discord.Member,
    name: str,
) -> tuple[discord.TextChannel, discord.Message]:
    """Create the corp text channel and pin the establishment embed."""
    # Sanitise channel name (Discord auto-lowercases and replaces spaces with hyphens)
    category = guild.get_channel(settings.CORP_CATEGORY_ID)
    channel = await guild.create_text_channel(
        name=f"corp-{name}",
        category=category,
        topic=f"🏢 {name} · Founded by {owner.display_name}",
        reason=f"Corporation established by {owner}",
    )

    # Set permissions: owner gets manage_channel so they can invite people etc.
    await channel.set_permissions(
        owner,
        manage_channels=False,
        manage_messages=True,
        send_messages=True,
        reason="Corp owner permissions",
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    gid = guild.id
    embed = discord.Embed(
        title=t(gid, "corps.setup.title", name=name),
        description=t(gid, "corps.setup.desc"),
        color=discord.Color.gold(),
        timestamp=now,
    )
    embed.add_field(name=t(gid, "corps.setup.founder"), value=owner.mention, inline=True)
    embed.add_field(
        name=t(gid, "corps.setup.established"),
        value=discord.utils.format_dt(now, style="F"),
        inline=True,
    )
    embed.set_thumbnail(url=owner.display_avatar.url)
    embed.set_footer(text=f"Corp ID: {owner.id}")

    msg = await channel.send(embed=embed)
    await msg.pin()

    return channel, msg


def _save_corp(guild_id: int, user_id: int, data: dict) -> None:
    """Write corporation data to Firestore."""
    ref = _get_corp_ref(guild_id, user_id)
    ref.set(data)
    # Ensure guild parent doc exists
    _db.collection("guilds").document(str(guild_id)).set(
        {"_exists": True}, merge=True
    )


def _get_corp(guild_id: int, user_id: int) -> dict | None:
    """Read corporation data from Firestore. Returns None if not found."""
    doc = _get_corp_ref(guild_id, user_id).get()
    return doc.to_dict() if doc.exists else None


def find_user_corp(guild_id: int, user_id: int) -> dict | None:
    """Find the corp a user belongs to, as owner or member. None if they're in none.

    Corps are keyed by the owner's id, so an owner is a direct lookup; members are
    found via the `members` array.
    """
    own = _get_corp(guild_id, user_id)
    if own:
        return own
    col = _db.collection("guilds").document(str(guild_id)).collection("corps")
    for doc in col.where("members", "array_contains", str(user_id)).stream():
        return doc.to_dict()
    return None


def _delete_corp(guild_id: int, user_id: int) -> None:
    """Delete a corporation record from Firestore."""
    _get_corp_ref(guild_id, user_id).delete()


# ── Confirmation UI ──────────────────────────────────────────────────────────

class CorpReplaceView(discord.ui.View):
    """DM view asking if the user wants to replace their existing corporation."""

    def __init__(self, cog: "Corps", guild: discord.Guild, owner: discord.Member, new_name: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.owner = owner
        self.new_name = new_name
        self.result: bool | None = None

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.confirming"), view=None
        )
        await self.cog._replace_corp(self.guild, self.owner, self.new_name)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.cancelled"), view=None
        )

    async def on_timeout(self) -> None:
        self.result = False


# ── Cog ──────────────────────────────────────────────────────────────────────

class Corps(commands.Cog, name="Corps"):
    """Corporation management system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /corpsetup ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="corpsetup",
        description="Establish a new corporation with its own text channel",
    )
    @app_commands.describe(name="Name for your corporation")
    async def corpsetup(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        gid = guild.id

        # Check if user already has a corporation
        existing = _get_corp(guild.id, member.id)

        if existing:
            # User already has a corp — DM them for confirmation
            old_name = existing.get("name", "Unknown")
            old_channel_id = existing.get("channel_id")

            view = CorpReplaceView(self, guild, member, name)

            try:
                dm_embed = discord.Embed(
                    title=t(gid, "corps.replace.title"),
                    description=t(gid, "corps.replace.desc",
                        guild=guild.name, old=old_name,
                        channel=old_channel_id, new=name),
                    color=discord.Color.orange(),
                )
                await member.send(embed=dm_embed, view=view)
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.check_dm"),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.no_dm"),
                    ephemeral=True,
                )
            return

        # No existing corp — create one
        await interaction.response.defer(ephemeral=True)

        channel, pin_msg = await _create_corp_channel(guild, member, name)

        # Auto-register as GK channel
        add_gk_channel(guild.id, channel.id)

        # Save to Firestore
        now = datetime.datetime.now(datetime.timezone.utc)
        _save_corp(guild.id, member.id, {
            "name": name,
            "owner_id": str(member.id),
            "owner_name": member.name,
            "channel_id": str(channel.id),
            "pin_message_id": str(pin_msg.id),
            "established_at": now.isoformat(),
            "members": [str(member.id)],
        })

        await interaction.followup.send(
            tp(gid, member.id, "corps.setup.done", name=name, channel=channel.mention),
            ephemeral=True,
        )
        log.info("%s established corporation '%s' (channel: %s)", member, name, channel.id)

    async def _replace_corp(
        self, guild: discord.Guild, owner: discord.Member, new_name: str
    ) -> None:
        """Delete old corp channel and create a replacement."""
        existing = _get_corp(guild.id, owner.id)
        if not existing:
            return

        # Try to delete old channel
        old_channel_id = existing.get("channel_id")
        if old_channel_id:
            old_channel = guild.get_channel(int(old_channel_id))
            if old_channel:
                try:
                    await old_channel.delete(reason=f"Corporation replaced by {owner}")
                except discord.Forbidden:
                    log.warning("No permission to delete old corp channel %s", old_channel_id)

        # Remove old channel from GK list
        if old_channel_id:
            remove_gk_channel(guild.id, int(old_channel_id))

        # Delete old Firestore record
        _delete_corp(guild.id, owner.id)

        # Create new corp
        channel, pin_msg = await _create_corp_channel(guild, owner, new_name)

        # Auto-register as GK channel
        add_gk_channel(guild.id, channel.id)

        now = datetime.datetime.now(datetime.timezone.utc)
        _save_corp(guild.id, owner.id, {
            "name": new_name,
            "owner_id": str(owner.id),
            "owner_name": owner.name,
            "channel_id": str(channel.id),
            "pin_message_id": str(pin_msg.id),
            "established_at": now.isoformat(),
            "members": [str(owner.id)],
        })

        try:
            await owner.send(t(guild.id, "corps.replace.done",
                name=new_name, guild=guild.name, channel=channel.id))
        except discord.Forbidden:
            pass

        log.info("%s replaced corporation with '%s' (channel: %s)", owner, new_name, channel.id)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error("Corps cog error: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                t(interaction.guild_id, "common.error"), ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Corps(bot))
