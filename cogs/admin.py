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
from cogs import perms
from api_auth import generate_link_code
from data import mod_version as mver
from data import policy as policy
from cost_guard import guard as cost_guard

log = logging.getLogger(__name__)


def is_admin():
    """Check: user must have Administrator permission or be the bot owner.

    Gates on the *real* invoker (mimic-safe) so an admin mimicking a higher-
    privileged user can't borrow their authority."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_admin_user(interaction)
    return app_commands.check(predicate)


def is_owner():
    """Check: user must be the bot owner (set via BOT_OWNER_ID in .env). Gates on
    the real invoker, so mimicking the owner does not pass this check."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_owner_user(interaction)
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
        embed.set_footer(text=f"Issued by {interaction.user}; session will run as {target.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s generated admin link code for %s (%s)", interaction.user, target, target.id)

    # ── /mimic ────────────────────────────────────────────────────────────────
    # Owner-only. Mimic lets the actor run every interaction AS another user, so it
    # is a powerful impersonation tool: restricted to the bot owner, and it refuses
    # to target another privileged user (owner/admin) so it can't be used to act
    # destructively as them. Sessions auto-expire (see bot.MIMIC_TTL).
    @app_commands.command(
        name="mimic", description="Act as another user for testing (Owner only)"
    )
    @app_commands.describe(target="The user to mimic")
    @is_owner()
    async def mimic(self, interaction: discord.Interaction, target: discord.Member) -> None:
        # Never mimic another privileged account (owner/admin) — avoids acting with
        # or against their authority and keeps the audit trail meaningful.
        if target.id == cfg.OWNER_ID or target.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You can't mimic another administrator or the owner.", ephemeral=True)
            return

        import time as _time
        from bot import MIMIC_TTL
        if not hasattr(self.bot, "mimic_map"):
            self.bot.mimic_map = {}
        self.bot.mimic_map[interaction.user.id] = (target, _time.time() + MIMIC_TTL)
        await interaction.response.send_message(
            f"🎭 You are now mimicking {target.mention}. Interactions will run as them "
            f"for {int(MIMIC_TTL // 60)} minutes, or until /unmimic.", ephemeral=True
        )
        # Audit on the REAL actor (interaction.user is the actor here — /mimic is
        # excluded from the swap), so the trail records who impersonated whom.
        log.warning("MIMIC: %s (%s) is now mimicking %s (%s)",
                    interaction.user, interaction.user.id, target, target.id)

    # ── /unmimic ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="unmimic", description="Stop mimicking another user (Owner only)"
    )
    @is_owner()
    async def unmimic(self, interaction: discord.Interaction) -> None:
        if hasattr(self.bot, "mimic_map") and interaction.user.id in self.bot.mimic_map:
            target, _ = self.bot.mimic_map.pop(interaction.user.id)
            await interaction.response.send_message(
                f"🎭 Stopped mimicking {target.mention}.", ephemeral=True
            )
            log.warning("MIMIC: %s (%s) stopped mimicking %s",
                        interaction.user, interaction.user.id, target)
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
        dll_bytes = None
        if dll is not None:
            dll_bytes = await dll.read()
            digest = hashlib.sha256(dll_bytes).hexdigest()

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
            mver.publish_version, version, digest, download_url, set_latest,
            str(interaction.user), dll_bytes
        )

        # If this became the latest, poke every live client to re-check now so
        # already-running clients gate without waiting for a restart.
        broadcast = rec.get("latest_version") == version.strip()
        if broadcast:
            try:
                import api_server
                api_server.broadcast_version_update()
            except Exception as exc:
                log.warning("Could not broadcast version update: %s", exc)

        embed = discord.Embed(
            title="✅ Mod version published",
            description=(
                f"**Version:** `{version}`\n"
                f"**SHA256:** `{digest}`\n"
                f"**Download:** {download_url}\n"
                f"**Latest now:** `{rec.get('latest_version')}`"
                + ("\n📡 Live clients poked to re-check." if broadcast else "")
                + ("\n🛡️ Attestation enabled (pristine DLL stored)."
                   if rec.get("versions", {}).get(version.strip(), {}).get("has_dll") else
                   "\n⚠️ Attestation OFF for this version. Upload the `dll` (not just a hash) to enable challenge-response anti-tamper.")
            ),
            color=discord.Color.green(),
        )
        if not cfg.KSP_VERSION_CHECK_ENABLED:
            embed.set_footer(text="⚠️ The version gate is disabled (KSP_VERSION_CHECK_ENABLED=false), so clients won't be blocked.")
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
                "ℹ️ No mod version has been published yet; the update gate is inactive.",
                ephemeral=True,
            )
            return
        versions = data.get("versions") or {}
        history = "\n".join(
            f"• `{v}`: `{(info.get('hash') or '')[:12]}…`"
            for v, info in versions.items()
        ) or "N/A"
        embed = discord.Embed(
            title="📦 KSP mod version registry",
            color=discord.Color.blurple(),
            description=(
                f"**Latest:** `{data.get('latest_version')}`\n"
                f"**Hash:** `{data.get('latest_hash')}`\n"
                f"**Download:** {data.get('download_url')}\n"
                f"**Gate:** {'on' if cfg.KSP_VERSION_CHECK_ENABLED else 'off (disabled in .env)'}\n"
                f"**Updated:** {data.get('updated_at', 'N/A')} by {data.get('updated_by', 'N/A')}\n\n"
                f"**Published versions:**\n{history}"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /policyversion ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="policyversion",
        description="Show or bump the Privacy/Terms version players must accept (Admin only)",
    )
    @app_commands.describe(
        version="New policy version to require (omit to just view the current one)",
        summary="Short note on what changed (optional, stored for the record)",
        privacy_url="Override the Privacy Policy URL shown to players (optional)",
        terms_url="Override the Terms of Service URL shown to players (optional)",
    )
    @is_admin()
    async def policyversion(
        self,
        interaction: discord.Interaction,
        version: int | None = None,
        summary: str | None = None,
        privacy_url: str | None = None,
        terms_url: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        current = await asyncio.to_thread(policy.get_version)

        # No version given → just report the current policy version.
        if version is None:
            data = await asyncio.to_thread(policy.get_config)
            embed = discord.Embed(
                title="📜 Policy version",
                color=discord.Color.blurple(),
                description=(
                    f"**Current required version:** `{current}`\n"
                    f"**Summary:** {data.get('summary') or 'N/A'}\n"
                    f"**Updated:** {data.get('updated_at', 'N/A')} by {data.get('updated_by', 'N/A')}\n\n"
                    f"Bump it with `/policyversion version:{current + 1}` to force every "
                    f"player who accepted an older version to re-accept."
                ),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Guard against accidentally lowering the version: clients only re-prompt
        # when the server version exceeds what they accepted, so going backwards
        # silently does nothing and is almost always a mistake.
        if version <= current:
            await interaction.followup.send(
                f"❌ New version (`{version}`) must be greater than the current one "
                f"(`{current}`). Re-consent is only triggered by a higher version.",
                ephemeral=True,
            )
            return

        rec = await asyncio.to_thread(
            policy.set_version, version, str(interaction.user),
            summary, privacy_url, terms_url,
        )

        # Poke live clients so they raise the re-consent gate now rather than on
        # their next restart.
        poked = False
        try:
            import api_server
            api_server.broadcast_policy_update()
            poked = True
        except Exception as exc:
            log.warning("Could not broadcast policy update: %s", exc)

        embed = discord.Embed(
            title="✅ Policy version bumped",
            color=discord.Color.green(),
            description=(
                f"**Now requires:** `{rec.get('version')}` (was `{current}`)\n"
                f"**Summary:** {rec.get('summary') or 'N/A'}\n"
                "Players who accepted an older version must now re-accept the "
                "Privacy Policy & Terms before the mod transmits again."
                + ("\n📡 Live clients poked to re-check." if poked else "")
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s bumped policy version to %s (was %s)",
                 interaction.user, version, current)

    # ── /costs ────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="costs",
        description="Show this month's estimated Gemini & Firebase spend (Admin only)",
    )
    @is_admin()
    async def costs(self, interaction: discord.Interaction) -> None:
        snap = cost_guard.snapshot()

        def fmt_budget(svc: dict) -> str:
            if svc["unlimited"]:
                return f"**${svc['usd']:.4f}** / unlimited"
            pct = (svc["usd"] / svc["budget"] * 100) if svc["budget"] else 0
            state = "🟢 active" if svc["ok"] else "🔴 capped"
            return f"**${svc['usd']:.4f}** / ${svc['budget']:.2f}  ({pct:.0f}%) · {state}"

        def human_bytes(n: int) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024 or unit == "GB":
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024

        g = snap["gemini"]
        f = snap["firebase"]

        # Per-component Firebase breakdown (skip rows that cost nothing).
        rows = []
        for label, count, usd in f["lines"]:
            if count == 0:
                continue
            qty = human_bytes(count) if "Storage" in label else f"{count:,}"
            rows.append(f"• {label}: {qty} → **${usd:.4f}**")
        breakdown = "\n".join(rows) or "• _no usage recorded yet_"

        guard_state = "on" if snap["enabled"] else "OFF (caps disabled in settings)"
        embed = discord.Embed(
            title=f"💸 Estimated service spend: {snap['month']}",
            color=discord.Color.green() if (g["ok"] and f["ok"]) else discord.Color.red(),
            description=(
                f"Cost guard: **{guard_state}**\n"
                "Figures are local estimates from usage, not Google's billing.\n\n"
                f"**🤖 Gemini** (soft-degrade)\n{fmt_budget(g)}\n\n"
                f"**🔥 Firebase** (hard-stop)\n{fmt_budget(f)}\n{breakdown}"
            ),
        )
        embed.set_footer(text="Budgets reset on the 1st (UTC). Tune prices/budgets in settings.py / .env.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
