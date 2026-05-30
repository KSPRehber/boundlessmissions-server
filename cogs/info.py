"""
cogs/info.py – Server and user info commands.
"""

import platform
import sys
import discord
from discord import app_commands
from discord.ext import commands


class Info(commands.Cog, name="Info"):
    """Informational commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="serverinfo", description="Display information about this server")
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        embed = discord.Embed(title=guild.name, description=guild.description or "No description", color=discord.Color.blurple())
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Owner", value=str(guild.owner), inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier} ({guild.premium_subscription_count} boosts)", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="D"), inline=True)
        embed.set_footer(text=f"Guild ID: {guild.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Display information about a user")
    @app_commands.describe(member="Member to inspect (defaults to yourself)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        top_role = member.top_role if member.top_role.name != "@everyone" else None
        embed = discord.Embed(title=str(member), color=member.color if member.color.value else discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Display Name", value=member.display_name, inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, style="D"), inline=True)
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, style="D") if member.joined_at else "Unknown", inline=True)
        embed.add_field(name="Top Role", value=top_role.mention if top_role else "None", inline=True)
        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        if roles:
            embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles[:15]) + ("…" if len(roles) > 15 else ""), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="botinfo", description="Display information about this bot")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        embed = discord.Embed(title=f"ℹ️ {bot.user.name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="Users", value=str(sum(g.member_count for g in bot.guilds)), inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Python", value=sys.version.split()[0], inline=True)
        embed.add_field(name="OS", value=platform.system(), inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
