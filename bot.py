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
            for cmd in list(self.tree.get_commands()):
                self.tree.remove_command(cmd.name)
                parent.add_command(cmd)
            self.tree.add_command(parent)

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

    async def on_ready(self) -> None:
        log.info("=" * 50)
        log.info("Bot ready!  Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Guilds: %d", len(self.guilds))
        log.info("=" * 50)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="/gk help",
                state="Unified Players of KSP Bot",
            )
        )

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
            await ctx.send("💥 An unexpected error occurred.")


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
        await bot.start(cfg.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
