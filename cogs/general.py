"""
cogs/general.py – General commands available to all users.
"""

import discord
from discord import app_commands
from discord.ext import commands
from config import cfg
from cogs import perms
from i18n import t, tp


def mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_mod_user(interaction)   # mimic-safe, per-guild mod role
    return app_commands.check(predicate)


class General(commands.Cog, name="General"):
    """General-purpose commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Prefix: !help ────────────────────────────────────────────────────────
    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context) -> None:
        """Show a help embed listing all available commands."""
        gid = ctx.guild.id if ctx.guild else None
        await ctx.send(embed=self._build_help_embed(gid))

    # ── Slash: /help ─────────────────────────────────────────────────────────
    @app_commands.command(name="help", description="List all available commands")
    async def help_slash(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        await interaction.response.send_message(
            embed=self._build_help_embed(gid, uid), ephemeral=True
        )

    def _build_help_embed(self, guild_id: int | None = None, user_id: int | None = None) -> discord.Embed:
        # Help is ephemeral → use personal language
        pfx = f"/{cfg.COMMAND_GROUP} " if cfg.COMMAND_GROUP else "/"
        _t = lambda key, **kw: tp(guild_id, user_id, key, **kw) if user_id else t(guild_id, key, **kw)
        embed = discord.Embed(
            title=_t("general.help.title"),
            description=_t("general.help.desc", pfx=pfx),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=_t("general.help.general"),
            value=(f"`{pfx}help` - List all available commands\n"
                   f"`{pfx}ping` - Check the bot's latency"),
            inline=False,
        )
        embed.add_field(
            name="Economy & Corps",
            value=(f"`{pfx}bal` - Check your KCoins balance\n"
                   f"`{pfx}pay` - Transfer KCoins to another user\n"
                   f"`{pfx}corpsetup` - Establish a corporation channel\n"
                   f"`{pfx}corpdisband` - Disband your corporation\n"
                   f"`{pfx}corprename` - Rename your corporation\n"
                   f"`{pfx}leaderboard` - View top players by XP/KCoins"),
            inline=False,
        )
        embed.add_field(
            name=_t("general.help.info"),
            value=(f"`{pfx}serverinfo` - Display server information\n"
                   f"`{pfx}userinfo` - Display user information\n"
                   f"`{pfx}botinfo` - Display bot statistics and uptime"),
            inline=False,
        )
        embed.add_field(
            name=_t("general.help.admin"),
            value=(f"`{pfx}reload` - Reload bot extensions/cogs\n"
                   f"`{pfx}shutdown` - Stop the bot safely\n"
                   f"`{pfx}announce` - Send an announcement to a channel\n"
                   f"`{pfx}setprefix` - Change the bot's command prefix"),
            inline=False,
        )
        if cfg.ENABLE_MOD_COMMANDS:
            embed.add_field(
                name=_t("general.help.mod"),
                value=(f"`{pfx}kick` - Kick a user from the server\n"
                       f"`{pfx}ban` - Ban a user from the server\n"
                       f"`{pfx}unban` - Unban a user\n"
                       f"`{pfx}mute` - Mute a user\n"
                       f"`{pfx}unmute` - Unmute a user\n"
                       f"`{pfx}purge` - Delete multiple messages\n"
                       f"`{pfx}warn` - Issue a warning to a user"),
                inline=False,
            )
        embed.set_footer(text=_t("general.help.footer"))
        return embed

    # ── /ping ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        latency_ms = round(self.bot.latency * 1000)
        color = (
            discord.Color.green()
            if latency_ms < 100
            else discord.Color.yellow()
            if latency_ms < 200
            else discord.Color.red()
        )
        embed = discord.Embed(
            title=tp(gid, uid, "general.ping.title"),
            description=tp(gid, uid, "general.ping.latency", ms=latency_ms),
            color=color,
        )
        await interaction.response.send_message(embed=embed)

    @commands.command(name="ping")
    async def ping_prefix(self, ctx: commands.Context) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! `{latency_ms} ms`")



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
