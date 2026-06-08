"""
bot.py – Main entry point.

Loads all cogs, syncs slash commands, and starts the bot.

Usage:
  Normal start (no sync):  .venv/bin/python bot.py
  Sync commands then run:  .venv/bin/python bot.py --sync

Only pass --sync when you have added or changed slash commands.
Doing it on every restart will hit Discord's rate limit.
"""

import asyncio
import logging
import sys
import discord
from discord import app_commands
from discord.ext import commands
from config import cfg

# Parse --sync flag before the bot starts
_SYNC_COMMANDS = "--sync" in sys.argv

log = logging.getLogger(__name__)

# ── Intents ──────────────────────────────────────────────────────────────────
# All intents enabled so the bot can react to every guild event.
# Admin bots typically need the full set; trim down for production if desired.
intents = discord.Intents.all()


# ── Bot subclass ─────────────────────────────────────────────────────────────
class GeneKermanBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=cfg.COMMAND_PREFIX,
            intents=intents,
            owner_id=cfg.OWNER_ID or None,
            help_command=None,  # We provide our own in cogs/general.py
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        """Called once before the bot connects – load cogs, optionally sync commands."""
        cog_modules = [
            "cogs.general",
            "cogs.admin",
            "cogs.info",
            "cogs.xp",
            "cogs.economy",
            "cogs.corps",
            "cogs.gkchannels",
            "cogs.screenshots",
            "cogs.contracts",
            "cogs.weeklymissions",
            "cogs.roles",
            "cogs.ksp_bridge",
        ]

        if cfg.ENABLE_MOD_COMMANDS:
            cog_modules.append("cogs.moderation")
            log.info("Moderation commands: ENABLED")
        else:
            log.info("Moderation commands: DISABLED (ENABLE_MOD_COMMANDS=false)")

        for module in cog_modules:
            try:
                await self.load_extension(module)
                log.info("Loaded cog: %s", module)
            except Exception as exc:
                log.error("Failed to load cog %s: %s", module, exc)

        # If a command group is configured, wrap all commands under it
        # This must happen every boot so the tree matches what Discord expects
        if cfg.COMMAND_GROUP:
            group_name = cfg.COMMAND_GROUP.lower()
            log.info("Command group active: /%s …", group_name)

            # Subclass to add GK channel gating
            from cogs.gkchannels import is_gk_channel, is_mod, get_gk_channel_mentions
            from i18n import tp

            class GKGroup(app_commands.Group):
                async def interaction_check(self, interaction: discord.Interaction) -> bool:
                    # DMs — allow
                    if interaction.guild is None:
                        return True
                    # Mods bypass
                    if isinstance(interaction.user, discord.Member) and is_mod(interaction.user):
                        return True
                    # GK channels — allow
                    if is_gk_channel(interaction.guild_id, interaction.channel_id):
                        return True
                    # Block with ephemeral message
                    channels_str = get_gk_channel_mentions(interaction.guild)
                    await interaction.response.send_message(
                        tp(interaction.guild_id, interaction.user.id,
                           "gk.cmd_wrong_channel", channels=channels_str),
                        ephemeral=True,
                    )
                    return False

            parent = GKGroup(
                name=group_name,
                description="Gene Kerman bot commands",
            )

            admin_group = app_commands.Group(
                name="admin", 
                description="Admin commands",
                default_permissions=discord.Permissions(administrator=True)
            )
            mod_group = app_commands.Group(
                name="mod", 
                description="Moderation commands",
                default_permissions=discord.Permissions(kick_members=True)
            )
            info_group = app_commands.Group(name="info", description="Info commands")

            for cmd in list(self.tree.get_commands()):
                self.tree.remove_command(cmd.name)
                
                cog_name = getattr(cmd.binding, "qualified_name", "").lower()
                
                cmd_is_admin = False
                cmd_is_mod = False
                
                if cmd.default_permissions:
                    if getattr(cmd.default_permissions, "administrator", False):
                        cmd_is_admin = True
                    if getattr(cmd.default_permissions, "kick_members", False) or getattr(cmd.default_permissions, "manage_guild", False):
                        cmd_is_mod = True
                if any("mod_only" in getattr(c, "__qualname__", "") for c in getattr(cmd, "checks", [])):
                    cmd_is_mod = True
                
                if cog_name == "admin" or cmd_is_admin:
                    admin_group.add_command(cmd)
                elif cog_name == "moderation" or cmd_is_mod:
                    mod_group.add_command(cmd)
                elif cog_name == "info":
                    info_group.add_command(cmd)
                else:
                    parent.add_command(cmd)

            if info_group.commands:
                parent.add_command(info_group)

            self.tree.add_command(parent)
            
            # Add mod and admin to top-level so their default_permissions are respected by Discord
            if admin_group.commands:
                self.tree.add_command(admin_group)
            if mod_group.commands:
                self.tree.add_command(mod_group)

        if _SYNC_COMMANDS:
            await self._sync_commands()
        else:
            log.info("Skipping command sync (pass --sync to force)")

    async def _sync_commands(self) -> None:
        """Push the current command tree to Discord's API."""
        if cfg.GUILD_IDS:
            for guild_id in cfg.GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d commands to guild %d", len(synced), guild_id)
                
            # Wipe global commands on Discord's side by clearing internal global commands
            # AFTER we already copied them to the guild.
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=None)
            log.info("Wiped any leftover global commands.")
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
            
        self.tree.on_error = self.on_app_command_error

    async def on_ready(self) -> None:
        log.info("=" * 50)
        log.info("Bot ready!  Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Guilds: %d", len(self.guilds))
        log.info("=" * 50)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="/g",
                state="Unified Players of KSP Bot",
            )
        )
        # Set bot user ID and instance for the KSP API server
        if cfg.KSP_API_ENABLED:
            from api_server import set_bot_user_id, set_bot_instance
            set_bot_user_id(self.user.id)
            set_bot_instance(self)
            log.info("KSP API: bot user ID set to %s", self.user.id)

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            pass  # silently ignore unknown prefix commands
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ Missing argument: `{error.param.name}`")
        else:
            log.error("Unhandled command error: %s", error, exc_info=True)
            try:
                maintainer = self.get_user(815228135049527297) or await self.fetch_user(815228135049527297)
                if maintainer:
                    await maintainer.send(f"⚠️ **Error in prefix command `{ctx.command.name if ctx.command else 'Unknown'}`:**\n```py\n{error}\n```")
            except Exception as exc:
                log.error("Failed to notify maintainer: %s", exc)
            await ctx.send("💥 An unexpected error occurred. The maintainer (<@815228135049527297>) has been pinged via DM.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.error("Unhandled app command error: %s", error, exc_info=True)
        try:
            maintainer = self.get_user(815228135049527297) or await self.fetch_user(815228135049527297)
            if maintainer:
                await maintainer.send(f"⚠️ **Error in slash command `{interaction.command.name if interaction.command else 'Unknown'}`:**\n```py\n{error}\n```")
        except Exception as exc:
            log.error("Failed to notify maintainer: %s", exc)
        msg = "💥 An unexpected error occurred. The maintainer (<@815228135049527297>) has been pinged via DM."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


# ── Runner ───────────────────────────────────────────────────────────────────
async def main() -> None:
    bot = GeneKermanBot()
    
    async def console_listener():
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            if line.strip().lower() == "stop":
                log.info("Stop command received from console. Shutting down...")
                await bot.close()
                break

    async with bot:
        asyncio.create_task(console_listener())

        # Start KSP API server alongside the bot
        if cfg.KSP_API_ENABLED:
            import uvicorn
            from api_server import app as api_app

            api_config = uvicorn.Config(
                api_app,
                host=cfg.API_HOST,
                port=cfg.API_PORT,
                log_level="info",
                access_log=False,
            )
            api_server = uvicorn.Server(api_config)
            asyncio.create_task(api_server.serve())
            log.info("KSP API server starting on %s:%d", cfg.API_HOST, cfg.API_PORT)
        else:
            log.info("KSP API server: DISABLED (KSP_API_ENABLED=false)")

        await bot.start(cfg.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
