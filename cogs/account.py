"""
cogs/account.py – `/b account`: manage your Boundless Missions account from Discord.

Three things, and the reason each lives here rather than on the website:

  • **See it.** A Discord user who has never opened the site still has an account
    (one is created the moment they link KSP), and this is where they can see what
    name it carries and what is attached to it.
  • **Log out everywhere.** The user's own privacy control, and the one action that
    genuinely belongs on a surface they can always reach — if a session is doing
    something they did not authorise, the website is exactly what they may not
    want to log in to.
  • **Link a website account.** The code is minted on the website (being signed in
    there proves control of the Google/email identity) and typed in here (running
    a slash command proves control of the Discord account). Doing both is the
    whole proof, because unlike a session link this creates no credential — it
    joins two identities the same person has just demonstrated they hold.

The attack that shape still has to answer is someone talking a victim into
entering the *attacker's* code, which would bind the victim's Discord to the
attacker's account. So the flow never links on the code alone: it shows whose
account is on the other end and waits for an explicit confirmation. A trick that
has to survive being named out loud is a much worse trick.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput

from api_auth import logout_all_devices
from cogs import perms
from data import accounts
from data.store import store

log = logging.getLogger(__name__)


def _account_embed(acct: dict, account_id: str) -> discord.Embed:
    """What this account is, in the terms a player recognises."""
    username = acct.get("username") or ""
    embed = discord.Embed(
        title="🛰️ Your Boundless Missions account",
        color=discord.Color.from_rgb(0x6A, 0xD2, 0x6A),
    )
    embed.add_field(
        name="Username",
        value=f"`{username}`" if username else "⚠️ *not chosen yet*",
        inline=True)
    embed.add_field(name="Display name",
                    value=acct.get("display_name") or "*not set*", inline=True)

    signins = []
    if acct.get("discord_id") or accounts.is_discord_account(account_id):
        signins.append("Discord")
    if acct.get("firebase_uid"):
        signins.append("Google / email")
    embed.add_field(name="Sign-in", value=", ".join(signins) or "*none*", inline=False)

    if not username:
        embed.add_field(
            name="⚠️ Choose a username",
            value=("Your Discord name wasn't available as a Boundless username, so "
                   "you need to pick one before you can sell crafts, offer "
                   "contracts or send ships. Open your account page on the website; "
                   "it only takes a moment and you only do it once."),
            inline=False)

    embed.set_footer(text=f"Account {account_id}")
    return embed


# The owner console and the guild-admin console resolve a website account's
# authority through `accounts/{id}.discord_id` — the field a join writes. So a
# join is the one place where "type this code for me" could hand somebody the
# console: talk a zero-activity owner/admin/mod Discord into joining an attacker's
# active website account and the web session inherits the role. The named-account
# confirmation makes that harder; this makes it impossible from this side. An
# account that holds authority is joined by the owner from the console, on purpose.
_AUTHORITY_REFUSAL = (
    "⛔ This Discord account holds the owner, admin or moderator role, so it can't "
    "be joined to a website account from a code, which would move the role's "
    "authority onto whichever account minted the code. Ask the bot owner to link "
    "it deliberately."
)


def _holds_authority(interaction: discord.Interaction) -> bool:
    # Every guild the bot is in, not the one this interaction happened in: the
    # website console honours the admin role from any guild, so a role holder
    # asked to run this from a second guild (or a DM, where the per-guild checks
    # simply see no member) must be refused just the same.
    if (perms.is_owner_user(interaction) or perms.is_admin_user(interaction)
            or perms.is_mod_user(interaction)):
        return True
    real = perms.real_user(interaction)
    return perms.holds_authority_anywhere(interaction.client, getattr(real, "id", 0))


class LinkCodeModal(Modal, title="Link a website account"):
    code = TextInput(label="6-digit code from your account page",
                     placeholder="123456", min_length=6, max_length=6)

    def __init__(self, account_id: str):
        super().__init__()
        self.account_id = account_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if _holds_authority(interaction):
            await interaction.followup.send(_AUTHORITY_REFUSAL, ephemeral=True)
            return
        entered = str(self.code.value).strip()

        target = await asyncio.to_thread(accounts.peek_link_challenge, entered)
        if target is None:
            await interaction.followup.send(
                "That code isn't valid, or it has expired. Get a fresh one from "
                "your account page on the website.", ephemeral=True)
            return

        # Peeked, not consumed — so nothing is joined until they say yes to a
        # named account. This is the whole defence against being talked into
        # entering somebody else's code.
        who = target.get("username") or target.get("display_name") or "an account"
        email = str(target.get("email") or "")
        masked = ""
        if "@" in email:
            name, _, domain = email.partition("@")
            masked = f"\n**Email:** `{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}`"

        # Say which name survives BEFORE they agree. Joining keeps the account
        # that holds the history, so a player who has been playing on Discord
        # keeps their Discord name and simply gains a Google button — and being
        # told that afterwards would read as having lost the other one.
        d_active = await asyncio.to_thread(accounts.has_activity, self.account_id)
        w_active = await asyncio.to_thread(accounts.has_activity, target["account_id"])
        mine = await asyncio.to_thread(accounts.get_account, self.account_id)
        my_name = (mine or {}).get("username") or ""

        if d_active and w_active:
            outcome = ("\n\n⚠️ **Both accounts have their own balance and history.** "
                       "Joining them would mean deciding which coins and crafts "
                       "survive, so they stay separate. Both keep working, so carry "
                       "on with whichever one you want to keep.")
        elif d_active or not w_active:
            outcome = (f"\n\nYour Discord account is the one that stays"
                       + (f", so you keep the name **{my_name}**" if my_name else "")
                       + ". You'll be able to sign in with Google as well.")
        else:
            outcome = (f"\n\nThe website account is the one that stays, so you'll "
                       f"be **{who}**. Your Discord becomes another way to sign in.")

        embed = discord.Embed(
            title="Link this account?",
            description=(
                f"You're about to join your Discord to the website account "
                f"**{who}**.{masked}{outcome}\n\n"
                "**If you don't recognise it, press Cancel.** Someone who talks you "
                "into entering their code would gain a Discord identity on their "
                "own account, and your name next to it."),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(
            embed=embed,
            view=ConfirmLinkView(self.account_id, entered, interaction.user.id),
            ephemeral=True)


class ConfirmLinkView(View):
    def __init__(self, discord_account_id: str, code: str, owner_id: int):
        super().__init__(timeout=120)
        self.discord_account_id = discord_account_id
        self.code = code
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Link it", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.defer(ephemeral=True)
        if _holds_authority(interaction):
            await interaction.followup.send(_AUTHORITY_REFUSAL, ephemeral=True)
            return

        target_id = await asyncio.to_thread(accounts.consume_link_challenge, self.code)
        if target_id is None:
            await interaction.followup.send(
                "That code expired while you were deciding. Get a new one.",
                ephemeral=True)
            return

        # `join_accounts`, not `link_discord`: the direction matters. Linking a
        # Discord id onto a fresh website account would leave every listing,
        # contract and coin still naming the Discord account nobody can sign into
        # any more. This keeps whichever side holds the history.
        code, message, _kept = await asyncio.to_thread(
            accounts.join_accounts, self.discord_account_id, target_id)

        if code in (accounts.JOIN_OK, accounts.JOIN_SAME):
            await interaction.followup.send(
                f"✅ {message} Sign in with Discord or with Google, same account "
                "either way.", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ {message}", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.send_message(
            "Cancelled. Nothing was linked.", ephemeral=True)
        self.stop()


class LogoutConfirmView(View):
    def __init__(self, account_id: str, owner_id: int):
        super().__init__(timeout=120)
        self.account_id = account_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Log out everywhere", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(logout_all_devices, self.account_id)
        await interaction.followup.send(
            "✅ Signed out everywhere. Every KSP install and browser session is "
            "logged out; nothing was deleted. Link again whenever you like.",
            ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class AccountView(View):
    def __init__(self, account_id: str, acct: dict, owner_id: int):
        super().__init__(timeout=180)
        self.account_id = account_id
        self.acct = acct
        self.owner_id = owner_id
        # Offer the link button only when there is something to link TO. An
        # account that already has a website sign-in has nothing to join.
        if acct.get("firebase_uid"):
            self.remove_item(self.link_website)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Link a website account", style=discord.ButtonStyle.primary)
    async def link_website(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.send_modal(LinkCodeModal(self.account_id))

    @discord.ui.button(label="Log out everywhere", style=discord.ButtonStyle.secondary)
    async def logout(self, interaction: discord.Interaction, _button: Button):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Log out of everything?",
                description=(
                    "Every KSP install and browser signed in as you is logged out. "
                    "**Nothing is deleted.** Your balance, crafts and contracts "
                    "are untouched, and you can link again straight away."),
                color=discord.Color.orange()),
            view=LogoutConfirmView(self.account_id, self.owner_id),
            ephemeral=True)


class Account(commands.Cog, name="Account"):
    """Your Boundless Missions account, from Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="account",
                          description="View and manage your Boundless Missions account")
    async def account(self, interaction: discord.Interaction):
        # Defer first: every branch below makes blocking Firestore calls, which
        # can outrun Discord's 3-second interaction window.
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)

        account_id = await asyncio.to_thread(accounts.account_for_discord, did)
        if account_id is None:
            await interaction.followup.send(
                "Couldn't reach your account just now. Try again in a moment.",
                ephemeral=True)
            return

        acct = await asyncio.to_thread(accounts.get_account, account_id)
        if acct is None:
            # No account yet. Deliberately does NOT create one: an account is made
            # when a player links KSP, which is the moment they have accepted the
            # terms in the mod — running a slash command is not that moment.
            await interaction.followup.send(
                "You don't have a Boundless Missions account yet. Link your game "
                "with `/b linkcode`, or sign up on the website; either one makes "
                "you an account.", ephemeral=True)
            return

        await interaction.followup.send(
            embed=_account_embed(acct, account_id),
            view=AccountView(account_id, acct, interaction.user.id),
            ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Account(bot))
