"""
cogs/tickets.py – Private support / report tickets.

A persistent "📩 Open a Ticket" button lives in TICKET_PANEL_CHANNEL_ID. Pressing
it shows a reason dropdown (report a user / report a bug / other), then a short
modal. On submit, a private channel is created under TICKET_CATEGORY_ID that only
the filer and the mods (MOD_ROLE_ID) can see — no outside access.

Other flows reuse `create_ticket()` to open tickets programmatically:
  • KSP account-sharing reports  (cogs/ksp_bridge.py)
  • Contract "sue" escalations    (cogs/contract_views.py)

Each ticket channel carries a "🔒 Close" button (mods or the opener may close).
The opener's id is stored in the channel topic so the close check survives a
bot restart.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, DynamicItem, Select, Modal, TextInput

import settings
from data.store import _db

try:
    from firebase_admin import firestore as _fs
except Exception:  # pragma: no cover - firestore always present in prod
    _fs = None

log = logging.getLogger(__name__)

# ── Ticket kinds ──────────────────────────────────────────────────────────────
# key → (emoji, human label, modal title, [(field_label, placeholder, long?)])
TICKET_KINDS = {
    "user": (
        "🚨", "Report a user", "🚨 Report a User",
        [
            ("Who are you reporting? (name / ID)", "e.g. SomeUser or 123456789012345678", False),
            ("What happened?", "Describe the issue, with any context…", True),
        ],
    ),
    "bug": (
        "🐛", "Report a bug / issue", "🐛 Report an Issue",
        [
            ("Short summary", "e.g. /g balance shows the wrong amount", False),
            ("Details / steps to reproduce", "What you did, what happened, what you expected…", True),
        ],
    ),
    "other": (
        "💬", "Something else", "💬 Open a Ticket",
        [
            ("Subject", "Short title for your ticket", False),
            ("Details", "Tell us what you need…", True),
        ],
    ),
}


# ── Ticket numbering ──────────────────────────────────────────────────────────

def _next_ticket_number(gid: int) -> int:
    """Atomically increment and return a per-guild ticket counter (Firestore txn)."""
    doc_ref = (_db.collection("guilds").document(str(gid))
               .collection("meta").document("tickets"))
    if _fs is None:
        # Fallback: best-effort read+write (single-process bot, rarely races).
        snap = doc_ref.get()
        cur = int((snap.to_dict() or {}).get("seq", 0)) if snap.exists else 0
        nxt = cur + 1
        doc_ref.set({"seq": nxt}, merge=True)
        return nxt

    txn = _db.transaction()

    @_fs.transactional
    def _run(transaction):
        snap = doc_ref.get(transaction=transaction)
        cur = int((snap.to_dict() or {}).get("seq", 0)) if snap.exists else 0
        nxt = cur + 1
        transaction.set(doc_ref, {"seq": nxt}, merge=True)
        return nxt

    return _run(txn)


def _ticket_opener_id(channel: discord.abc.GuildChannel) -> int | None:
    """Read the opener's user id back out of a ticket channel's topic."""
    topic = getattr(channel, "topic", None) or ""
    for part in topic.split("|"):
        if part.startswith("opener="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


# ── Programmatic ticket creation (shared by all flows) ────────────────────────

async def create_ticket(
    client: discord.Client,
    guild: discord.Guild,
    *,
    opener_id: int | None,
    kind: str,
    title: str,
    description: str = "",
    color: discord.Color | None = None,
    subject_user_id: int | None = None,
    extra_user_ids: list[int] | None = None,
    extra_embeds: list[discord.Embed] | None = None,
    extra_view: View | None = None,
    files: list[discord.File] | None = None,
    ping_mods: bool = True,
) -> discord.TextChannel | None:
    """Create a private ticket channel under TICKET_CATEGORY_ID and post the opening
    message. Visible only to @mods, the opener (if any), and any extra_user_ids.

    `opener_id=None` makes a **mods-only** ticket (used for auto-flagged anti-cheat
    reports where the suspect must NOT see it); only mods can then close it.
    `subject_user_id` is shown for context but is NOT granted access. Returns the
    channel, or None if the ticket system is unconfigured / creation failed."""
    cat_id = settings.TICKET_CATEGORY_ID
    if not cat_id:
        log.warning("create_ticket called but TICKET_CATEGORY_ID is unset")
        return None

    category = guild.get_channel(cat_id)
    if not isinstance(category, discord.CategoryChannel):
        try:
            category = await guild.fetch_channel(cat_id)
        except Exception as exc:
            log.warning("Ticket category %s not found: %s", cat_id, exc)
            category = None

    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_channels=True, manage_messages=True,
            embed_links=True, attach_files=True,
        ),
    }
    mod_role = guild.get_role(settings.MOD_ROLE_ID) if settings.MOD_ROLE_ID else None
    if mod_role:
        overwrites[mod_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True)

    member_ids = ([int(opener_id)] if opener_id else []) + [int(u) for u in (extra_user_ids or [])]
    for uid in dict.fromkeys(member_ids):  # de-dupe, preserve order
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        if member:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True)

    num = await asyncio.to_thread(_next_ticket_number, guild.id)
    chan_name = f"ticket-{num:04d}"
    try:
        channel = await guild.create_text_channel(
            name=chan_name,
            category=category,
            overwrites=overwrites,
            topic=f"GKTicket|opener={opener_id or ''}|kind={kind}",
            reason=f"Ticket #{num:04d} ({kind}) opened",
        )
    except Exception as exc:
        log.error("Could not create ticket channel for %s: %s", opener_id, exc)
        return None

    emoji = TICKET_KINDS.get(kind, ("🎫",))[0]
    e = discord.Embed(
        title=f"{emoji} {title}",
        description=description or None,
        color=color or discord.Color.blurple(),
    )
    e.set_footer(text=f"Ticket #{num:04d}")
    opener = guild.get_member(int(opener_id)) if opener_id else None
    if opener:
        e.set_author(name=str(opener), icon_url=getattr(opener.display_avatar, "url", None))
    if subject_user_id:
        subj = guild.get_member(int(subject_user_id))
        e.add_field(name="Reported user",
                    value=(f"{subj.mention} (`{subject_user_id}`)" if subj else f"`{subject_user_id}`"),
                    inline=False)

    content_bits = []
    if opener:
        content_bits.append(opener.mention)
    if ping_mods and mod_role:
        content_bits.append(mod_role.mention)
    content = " ".join(content_bits) or None

    try:
        await channel.send(content=content, embed=e, view=TicketControlView(),
                           allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        embeds = list(extra_embeds or [])
        # Attach the action view (e.g. a contract ModReviewView) to the final
        # embed so the buttons sit with their context; otherwise post it alone.
        if embeds:
            for emb in embeds[:-1]:
                await channel.send(embed=emb)
            await channel.send(embed=embeds[-1], view=extra_view,
                               files=files or [])
        elif extra_view is not None or files:
            await channel.send(view=extra_view, files=files or [])
    except Exception as exc:
        log.warning("Ticket %s created but opening post failed: %s", chan_name, exc)

    log.info("Opened ticket %s (kind=%s) for user %s in guild %s",
             chan_name, kind, opener_id, guild.id)
    return channel


# ── "Open a Ticket" panel button ──────────────────────────────────────────────

class OpenTicketButton(DynamicItem[Button], template=r"gk_ticket_open"):
    def __init__(self):
        super().__init__(Button(
            label="📩 Open a Ticket", style=discord.ButtonStyle.blurple,
            custom_id="gk_ticket_open"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Tickets can only be opened inside the server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "What is your ticket about?",
            view=_ReasonView(), ephemeral=True)


class _ReasonView(View):
    """Ephemeral reason dropdown shown after the panel button is pressed."""
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(_ReasonSelect())


class _ReasonSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji)
            for key, (emoji, label, *_rest) in TICKET_KINDS.items()
        ]
        super().__init__(placeholder="Choose a reason…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        kind = self.values[0]
        await interaction.response.send_modal(TicketModal(kind))


class TicketModal(Modal):
    """Collects the ticket subject + details, then opens the private channel."""
    def __init__(self, kind: str):
        self.kind = kind
        emoji, label, modal_title, fields = TICKET_KINDS.get(kind, TICKET_KINDS["other"])
        super().__init__(title=modal_title)
        self._inputs: list[TextInput] = []
        for flabel, placeholder, long in fields:
            ti = TextInput(
                label=flabel[:45],
                placeholder=placeholder[:100],
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                required=True,
                max_length=1000 if long else 200,
            )
            self.add_item(ti)
            self._inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        emoji, label, modal_title, fields = TICKET_KINDS.get(self.kind, TICKET_KINDS["other"])
        subject = self._inputs[0].value.strip()
        details = self._inputs[1].value.strip() if len(self._inputs) > 1 else ""

        first_label = fields[0][0]
        desc = f"**{first_label}**\n{subject}\n\n"
        if len(fields) > 1:
            desc += f"**{fields[1][0]}**\n{details}"

        channel = await create_ticket(
            interaction.client, interaction.guild,
            opener_id=interaction.user.id,
            kind=self.kind,
            title=label,
            description=desc,
            color=discord.Color.orange() if self.kind == "user" else discord.Color.blurple(),
        )
        if channel is None:
            await interaction.followup.send(
                "⚠️ Couldn't open a ticket right now — the ticket system may be "
                "misconfigured. Please ping a moderator directly.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Your ticket has been opened: {channel.mention}\n"
            "Only you and the moderators can see it.", ephemeral=True)


# ── Ticket close control ──────────────────────────────────────────────────────

class CloseTicketButton(DynamicItem[Button], template=r"gk_ticket_close"):
    def __init__(self):
        super().__init__(Button(
            label="🔒 Close ticket", style=discord.ButtonStyle.red,
            custom_id="gk_ticket_close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        from cogs.gkchannels import is_mod
        channel = interaction.channel
        opener_id = _ticket_opener_id(channel)
        member = interaction.user
        allowed = (isinstance(member, discord.Member) and is_mod(member)) \
            or (opener_id is not None and member.id == opener_id)
        if not allowed:
            await interaction.response.send_message(
                "Only a moderator or the person who opened this ticket can close it.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"🔒 Ticket closed by {member.mention}. Deleting in 5 seconds…",
                color=discord.Color.greyple()))
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {member}")
        except Exception as exc:
            log.warning("Could not delete ticket channel %s: %s", channel.id, exc)


class TicketControlView(View):
    """Attached to a ticket's opening message: lets mods/opener close it."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseTicketButton())


class TicketPanelView(View):
    """The persistent panel posted in TICKET_PANEL_CHANNEL_ID."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenTicketButton())


def _panel_embed() -> discord.Embed:
    return discord.Embed(
        title="📩 Need help or want to report something?",
        description=(
            "Open a **private ticket** that only you and the moderators can see.\n\n"
            "🚨 **Report a user** — rule-breaking, account sharing, harassment…\n"
            "🐛 **Report a bug / issue** — something in the bot or KSP mod is broken.\n"
            "💬 **Something else** — questions, requests, anything.\n\n"
            "Press the button below and pick a reason."
        ),
        color=discord.Color.blurple(),
    )


async def _find_existing_panel(channel: discord.TextChannel, bot_user_id: int):
    """Return the bot's existing panel message in the channel (by its Open-Ticket
    button custom_id), or None — so we never post a duplicate panel on restart."""
    try:
        async for msg in channel.history(limit=50):
            if msg.author.id != bot_user_id:
                continue
            for row in msg.components:
                for comp in getattr(row, "children", []):
                    if getattr(comp, "custom_id", None) == "gk_ticket_open":
                        return msg
    except Exception as exc:
        log.warning("Could not scan ticket panel channel %s: %s", channel.id, exc)
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog, name="Tickets"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_ensured = False

    async def _resolve_panel_channel(self):
        ch_id = settings.TICKET_PANEL_CHANNEL_ID
        if not ch_id:
            return None
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ch_id)
            except Exception as exc:
                log.warning("Ticket panel channel %s not found: %s", ch_id, exc)
                return None
        return channel

    @commands.Cog.listener()
    async def on_ready(self):
        # Auto-post the panel once per process so it's always present without an
        # admin having to run /ticketpanel. Idempotent: skips if one already exists.
        if self._panel_ensured:
            return
        self._panel_ensured = True
        channel = await self._resolve_panel_channel()
        if channel is None:
            return
        if await _find_existing_panel(channel, self.bot.user.id):
            log.info("Ticket panel already present in #%s", getattr(channel, "name", channel.id))
            return
        try:
            await channel.send(embed=_panel_embed(), view=TicketPanelView())
            log.info("Auto-posted ticket panel in #%s", getattr(channel, "name", channel.id))
        except discord.Forbidden:
            log.warning("Missing permission to post the ticket panel in channel %s "
                        "(need View Channel + Send Messages + Embed Links)", channel.id)
        except Exception as exc:
            log.warning("Could not auto-post ticket panel: %s", exc)

    @app_commands.command(name="ticketpanel",
                          description="Post the 'Open a Ticket' panel in the ticket channel")
    @app_commands.default_permissions(administrator=True)
    async def ticketpanel(self, interaction: discord.Interaction):
        """(Admin) Post a fresh ticket panel message."""
        ch_id = settings.TICKET_PANEL_CHANNEL_ID
        if not ch_id:
            await interaction.response.send_message(
                "❌ `TICKET_PANEL_CHANNEL_ID` is not set in settings.py.", ephemeral=True)
            return
        channel = await self._resolve_panel_channel()
        if channel is None:
            await interaction.response.send_message(
                f"❌ Could not find the panel channel (`{ch_id}`).", ephemeral=True)
            return
        try:
            await channel.send(embed=_panel_embed(), view=TicketPanelView())
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to post in {channel.mention} "
                "(need View Channel + Send Messages + Embed Links).", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Ticket panel posted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
    # Persistent components survive restarts via their custom_id.
    bot.add_dynamic_items(OpenTicketButton, CloseTicketButton)
