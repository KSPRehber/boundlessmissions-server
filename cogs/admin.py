"""
cogs/admin.py – Administrative commands (bot owner / server admins only).
"""

import asyncio
import hashlib
import logging
import discord
from discord import app_commands
from discord.ext import commands
from config import cfg
from api_auth import generate_link_code
from data import mod_version as mver

log = logging.getLogger(__name__)


def is_admin():
    """Check: user must have Administrator permission or be the bot owner."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == cfg.OWNER_ID:
            return True
        if isinstance(interaction.user, discord.Member):
            return interaction.user.guild_permissions.administrator
        return False
    return app_commands.check(predicate)


def is_owner():
    """Check: user must be the bot owner (set via BOT_OWNER_ID in .env)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == cfg.OWNER_ID
    return app_commands.check(predicate)


class Admin(commands.Cog, name="Admin"):
    """Commands restricted to server admins and the bot owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /announce ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="announce",
        description="Send an announcement embed to a channel (Admin only)",
    )
    @app_commands.describe(
        channel="Target channel",
        title="Embed title",
        message="Embed body text",
        color="Hex color code e.g. #5865F2 (optional)",
    )
    @is_admin()
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        message: str,
        color: str = "#5865F2",
    ) -> None:
        try:
            hex_color = int(color.lstrip("#"), 16)
        except ValueError:
            hex_color = 0x5865F2

        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color(hex_color),
        )
        embed.set_footer(text=f"Announced by {interaction.user}")
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Announcement sent to {channel.mention}", ephemeral=True
        )
        log.info("%s sent announcement to #%s", interaction.user, channel.name)

    # ── /reload ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="reload", description="Reload a cog without restarting (Owner only)"
    )
    @app_commands.describe(cog="Cog module path e.g. cogs.general")
    @is_owner()
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        try:
            await self.bot.reload_extension(cog)
            await interaction.response.send_message(
                f"🔄 Reloaded `{cog}`", ephemeral=True
            )
            log.info("%s reloaded %s", interaction.user, cog)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Failed to reload `{cog}`: {exc}", ephemeral=True
            )
            log.error("Reload error for %s: %s", cog, exc)

    # ── /shutdown ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="shutdown", description="Gracefully shut the bot down (Owner only)"
    )
    @is_owner()
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("👋 Shutting down…", ephemeral=True)
        log.warning("%s initiated shutdown", interaction.user)
        await self.bot.close()

    # ── /setprefix ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="setprefix",
        description="Change the bot's prefix command character (Admin only)",
    )
    @app_commands.describe(prefix="New prefix character(s)")
    @is_admin()
    async def setprefix(
        self, interaction: discord.Interaction, prefix: str
    ) -> None:
        self.bot.command_prefix = prefix
        await interaction.response.send_message(
            f"✅ Prefix changed to `{prefix}`", ephemeral=True
        )
        log.info("%s changed prefix to '%s'", interaction.user, prefix)

    # ── /linkas ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="linkas",
        description="Generate a KSP link code that logs in as another user (Admin only)",
    )
    @app_commands.describe(target="The user whose KSP session to assume")
    @is_admin()
    async def linkas(self, interaction: discord.Interaction, target: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        code = await asyncio.to_thread(
            generate_link_code, interaction.guild_id, target.id, target.display_name
        )
        embed = discord.Embed(
            title="🔧 Admin KSP Link Code",
            description=f"Linking as **{target.display_name}** (`{target.id}`).\n\nEnter this code in KSP:\n\n# `{code}`\n\n⏰ Expires in 10 minutes.",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Issued by {interaction.user} — session will run as {target.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s generated admin link code for %s (%s)", interaction.user, target, target.id)

    # ── /mimic ────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="mimic", description="Act as another user for testing (Admin only)"
    )
    @app_commands.describe(target="The user to mimic")
    @is_admin()
    async def mimic(self, interaction: discord.Interaction, target: discord.Member) -> None:
        if not hasattr(self.bot, "mimic_map"):
            self.bot.mimic_map = {}
        self.bot.mimic_map[interaction.user.id] = target
        await interaction.response.send_message(
            f"🎭 You are now mimicking {target.mention}. Interactions will run as them.", ephemeral=True
        )
        log.info("%s is now mimicking %s", interaction.user, target)

    # ── /unmimic ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="unmimic", description="Stop mimicking another user (Admin only)"
    )
    @is_admin()
    async def unmimic(self, interaction: discord.Interaction) -> None:
        if hasattr(self.bot, "mimic_map") and interaction.user.id in self.bot.mimic_map:
            target = self.bot.mimic_map.pop(interaction.user.id)
            await interaction.response.send_message(
                f"🎭 Stopped mimicking {target.mention}.", ephemeral=True
            )
            log.info("%s stopped mimicking %s", interaction.user, target)
        else:
            await interaction.response.send_message(
                "❌ You are not mimicking anyone.", ephemeral=True
            )

    # ── /publishversion ───────────────────────────────────────────────────────
    @app_commands.command(
        name="publishversion",
        description="Register a KSP mod DLL version + hash for the update gate (Admin only)",
    )
    @app_commands.describe(
        version="Version label, e.g. 1.2.0",
        download_url="Where players download this version",
        dll="Upload GeneKerman.dll to auto-compute its SHA256 (preferred)",
        sha256="Paste the DLL's SHA256 instead of uploading (optional)",
        set_latest="Make this the required latest version (default: yes)",
    )
    @is_admin()
    async def publishversion(
        self,
        interaction: discord.Interaction,
        version: str,
        download_url: str,
        dll: discord.Attachment | None = None,
        sha256: str | None = None,
        set_latest: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        digest = (sha256 or "").strip().lower()
        if dll is not None:
            data = await dll.read()
            digest = hashlib.sha256(data).hexdigest()

        if not digest:
            await interaction.followup.send(
                "❌ Provide either a `dll` upload or a `sha256` hash.", ephemeral=True
            )
            return
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            await interaction.followup.send(
                "❌ That doesn't look like a SHA256 hash (expected 64 hex chars).", ephemeral=True
            )
            return

        rec = await asyncio.to_thread(
            mver.publish_version, version, digest, download_url, set_latest, str(interaction.user)
        )

        embed = discord.Embed(
            title="✅ Mod version published",
            description=(
                f"**Version:** `{version}`\n"
                f"**SHA256:** `{digest}`\n"
                f"**Download:** {download_url}\n"
                f"**Latest now:** `{rec.get('latest_version')}`"
            ),
            color=discord.Color.green(),
        )
        if not cfg.KSP_VERSION_CHECK_ENABLED:
            embed.set_footer(text="⚠️ The version gate is disabled (KSP_VERSION_CHECK_ENABLED=false) — clients won't be blocked.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s published mod version %s (%s, latest=%s)",
                 interaction.user, version, digest[:12], rec.get("latest_version"))

    # ── /versioninfo ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="versioninfo",
        description="Show the currently published latest KSP mod version (Admin only)",
    )
    @is_admin()
    async def versioninfo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        data = await asyncio.to_thread(mver.get_config)
        if not data or not data.get("latest_hash"):
            await interaction.followup.send(
                "ℹ️ No mod version has been published yet — the update gate is inactive.",
                ephemeral=True,
            )
            return
        versions = data.get("versions") or {}
        history = "\n".join(
            f"• `{v}` — `{(info.get('hash') or '')[:12]}…`"
            for v, info in versions.items()
        ) or "—"
        embed = discord.Embed(
            title="📦 KSP mod version registry",
            color=discord.Color.blurple(),
            description=(
                f"**Latest:** `{data.get('latest_version')}`\n"
                f"**Hash:** `{data.get('latest_hash')}`\n"
                f"**Download:** {data.get('download_url')}\n"
                f"**Gate:** {'on' if cfg.KSP_VERSION_CHECK_ENABLED else 'off (disabled in .env)'}\n"
                f"**Updated:** {data.get('updated_at', '—')} by {data.get('updated_by', '—')}\n\n"
                f"**Published versions:**\n{history}"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
        else:
            log.error("Admin cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "💥 An error occurred.", ephemeral=True
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
