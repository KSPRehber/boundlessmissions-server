"""
cogs/general.py – General commands available to all users.
"""

import discord
from discord import app_commands
from discord.ext import commands
from config import cfg
from i18n import t, tp, get_user_lang, set_user_lang, get_server_lang, set_server_lang, LANG_NAMES


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
            value=f"`{pfx}help` · `{pfx}ping` · `{pfx}langswitch` · `{pfx}tsl`",
            inline=False,
        )
        embed.add_field(
            name=_t("general.help.info"),
            value=f"`{pfx}serverinfo` · `{pfx}userinfo` · `{pfx}botinfo`",
            inline=False,
        )
        embed.add_field(
            name=_t("general.help.admin"),
            value=f"`{pfx}reload` · `{pfx}shutdown` · `{pfx}announce` · `{pfx}setprefix`",
            inline=False,
        )
        if cfg.ENABLE_MOD_COMMANDS:
            embed.add_field(
                name=_t("general.help.mod"),
                value=f"`{pfx}kick` · `{pfx}ban` · `{pfx}unban` · `{pfx}mute` · `{pfx}unmute` · `{pfx}purge` · `{pfx}warn`",
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

    # ── /langswitch (personal) ───────────────────────────────────────────────
    @app_commands.command(name="langswitch", description="Toggle your personal bot language (TR/EN)")
    async def langswitch(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        current = get_user_lang(gid, uid)
        new_lang = "en" if current == "tr" else "tr"
        set_user_lang(gid, uid, new_lang)
        await interaction.response.send_message(
            tp(gid, uid, "lang.personal.switched", lang_name=LANG_NAMES[new_lang]),
            ephemeral=True,
        )

    # ── /tsl (server language, mod-only) ─────────────────────────────────────
    @app_commands.command(name="tsl", description="Toggle server-wide bot language (TR/EN) — Mod only")
    @app_commands.checks.has_permissions(administrator=True)
    async def tsl(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        current = get_server_lang(gid)
        new_lang = "en" if current == "tr" else "tr"
        set_server_lang(gid, new_lang)
        # Public message — use new server language
        await interaction.response.send_message(
            t(gid, "lang.server.switched", lang_name=LANG_NAMES[new_lang]),
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
