import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select

import settings
from data.store import store
from i18n import tp
from cogs.moderation import mod_only

log = logging.getLogger(__name__)

async def sync_user_levels(bot: commands.Bot, uid: int) -> set[int]:
    """Scans all guilds to find the level roles the user has.
    Updates the store if it finds new ones, and returns the set of all unlocked levels.
    """
    found_levels = set()
    
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
            
        for level, r_info in settings.LEVEL_ROLES.items():
            role_id = r_info[0]
            if any(r.id == role_id for r in member.roles):
                found_levels.add(level)
                    
    # Ensure store is updated if we found new levels
    shared_guilds = [g.id for g in bot.guilds if g.get_member(uid)]
    if shared_guilds:
        gid = shared_guilds[0]
        for lvl in found_levels:
            await store.add_unlocked_level(gid, uid, lvl)
            
    # Also grab whatever was already stored
    all_unlocked = set(found_levels)
    for g in bot.guilds:
        user_data = store.get_user(g.id, uid)
        s_levels = user_data.get("unlocked_levels", [])
        all_unlocked.update(s_levels)
        
        # Legacy fallback
        old_max = user_data.get("max_unlocked_level", 0)
        if old_max > 0:
            for l in range(1, old_max + 1):
                all_unlocked.add(l)
            
    return all_unlocked


def get_equipped_levels(bot: commands.Bot, uid: int) -> set[int]:
    """Returns a set of level integers the user currently has equipped in any shared guild."""
    equipped = set()
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
        for level, r_info in settings.LEVEL_ROLES.items():
            if any(r.id == r_info[0] for r in member.roles):
                equipped.add(level)
    return equipped


class LevelSelector(Select):
    def __init__(self, unlocked: set[int], equipped: set[int]):
        self.unlocked = unlocked
        options = []
        for lvl in sorted(list(unlocked)):
            if lvl in settings.LEVEL_ROLES:
                r_info = settings.LEVEL_ROLES[lvl]
                options.append(discord.SelectOption(
                    label=r_info[1],
                    description=r_info[2][:100],
                    value=str(lvl),
                    default=(lvl in equipped)
                ))
                
        if not options:
            options.append(discord.SelectOption(label="None unlocked", value="0"))
            
        super().__init__(
            placeholder="Select KSP Titles to equip...",
            min_values=0,
            max_values=len(options) if options[0].value != "0" else 1,
            options=options,
            custom_id="level_role_dropdown"
        )

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        
        if "0" in self.values and len(self.values) == 1:
            await interaction.response.send_message("No titles unlocked yet.", ephemeral=True)
            return

        selected_levels = set(int(v) for v in self.values)
        
        # Verify they aren't cheating the client
        unlocked = await sync_user_levels(interaction.client, uid)
        if any(lvl not in unlocked for lvl in selected_levels):
            await interaction.response.send_message("❌ You selected a level you haven't unlocked.", ephemeral=True)
            return
            
        added_count = 0
        removed_count = 0
        
        for guild in interaction.client.guilds:
            member = guild.get_member(uid)
            if not member:
                continue
                
            roles_to_add = []
            roles_to_remove = []
            
            for lvl, r_info in settings.LEVEL_ROLES.items():
                role_id = r_info[0]
                role_obj = guild.get_role(role_id)
                if not role_obj:
                    continue
                    
                has_role = any(r.id == role_id for r in member.roles)
                
                if lvl in selected_levels:
                    if not has_role:
                        roles_to_add.append(role_obj)
                else:
                    if has_role:
                        roles_to_remove.append(role_obj)
                        
            try:
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="User unequipped level titles via DM")
                    removed_count += len(roles_to_remove)
                if roles_to_add:
                    await member.add_roles(*roles_to_add, reason="User equipped level titles via DM")
                    added_count += len(roles_to_add)
            except discord.Forbidden:
                log.warning("Missing permissions to manage roles for %s in %s", uid, guild.id)
                
        await interaction.response.send_message(
            f"✅ Roles updated! Equipped **{len(selected_levels)}** title(s).",
            ephemeral=True
        )


class LevelRoleView(View):
    def __init__(self, unlocked: set[int], equipped: set[int]):
        super().__init__(timeout=None)
        self.add_item(LevelSelector(unlocked, equipped))


class GenericRoleView(View):
    """A generic persistent view to catch old dropdown interactions."""
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.select(custom_id="level_role_dropdown", options=[discord.SelectOption(label="loading", value="0")])
    async def fallback_callback(self, interaction: discord.Interaction, select: Select):
        uid = interaction.user.id
        unlocked = await sync_user_levels(interaction.client, uid)
        equipped = get_equipped_levels(interaction.client, uid)
        
        proper_selector = LevelSelector(unlocked, equipped)
        proper_selector.values = select.values
        await proper_selector.callback(interaction)


class ModLevelRemoveSelector(Select):
    def __init__(self, target: discord.Member, unlocked: set[int]):
        self.target = target
        options = [discord.SelectOption(label="Remove ALL Levels", value="0", description="Revoke all level titles from this user")]
        
        for lvl in sorted(list(unlocked)):
            if lvl in settings.LEVEL_ROLES:
                r_info = settings.LEVEL_ROLES[lvl]
                options.append(discord.SelectOption(
                    label=f"Level {lvl}: {r_info[1]}",
                    value=str(lvl)
                ))
                
        super().__init__(
            placeholder=f"Select level(s) to remove from {target.display_name}...",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        selected = set(int(v) for v in self.values)
        guild = interaction.guild
        uid = self.target.id
        
        # Check if they selected 0 (Remove ALL)
        remove_all = 0 in selected
        
        removed_roles = []
        if remove_all:
            await store.remove_unlocked_level(guild.id, uid, 0)
            # Remove all level roles from discord member
            for r_info in settings.LEVEL_ROLES.values():
                role = guild.get_role(r_info[0])
                if role and role in self.target.roles:
                    removed_roles.append(role)
        else:
            for lvl in selected:
                await store.remove_unlocked_level(guild.id, uid, lvl)
                if lvl in settings.LEVEL_ROLES:
                    role = guild.get_role(settings.LEVEL_ROLES[lvl][0])
                    if role and role in self.target.roles:
                        removed_roles.append(role)
                        
        if removed_roles:
            try:
                await self.target.remove_roles(*removed_roles, reason=f"Mod {interaction.user} removed level roles")
            except discord.Forbidden:
                await interaction.response.send_message("❌ Database updated, but I lack permissions to remove Discord roles.", ephemeral=True)
                return
                
        # Disable select
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        
        await interaction.followup.send(
            f"✅ Successfully removed **{'ALL' if remove_all else len(selected)}** level(s) from {self.target.mention}.",
            ephemeral=True
        )


class ModLevelRemoveView(View):
    def __init__(self, target: discord.Member, unlocked: set[int]):
        super().__init__(timeout=300)
        self.add_item(ModLevelRemoveSelector(target, unlocked))


class Roles(commands.Cog, name="Roles"):
    """Role management and level titles."""
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="roles",
        description="Open the KSP Title selector to manage your equipped roles",
    )
    async def roles_cmd(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        
        unlocked = await sync_user_levels(self.bot, uid)
        if not unlocked:
            await interaction.response.send_message(
                "❌ You have not unlocked any KSP titles yet. Complete missions or upload screenshots to earn them!",
                ephemeral=True
            )
            return
            
        equipped = get_equipped_levels(self.bot, uid)
        view = LevelRoleView(unlocked, equipped)
        
        embed = discord.Embed(
            title="🎖️ KSP Title Selector",
            description="Select which KSP achievement titles you want to display on your profile. You can equip multiple titles!",
            color=discord.Color.blue()
        )
        
        try:
            await interaction.user.send(embed=embed, view=view)
            await interaction.response.send_message(
                "✅ Check your DMs for the title selector!", 
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot send you a DM. Please enable direct messages from server members.", 
                ephemeral=True
            )

    @app_commands.command(
        name="removeroles",
        description="[MOD] Remove KSP level roles from a user"
    )
    @app_commands.describe(target="The user to remove level roles from")
    @mod_only()
    async def removeroles_cmd(self, interaction: discord.Interaction, target: discord.Member) -> None:
        unlocked = await sync_user_levels(self.bot, target.id)
        if not unlocked:
            await interaction.response.send_message(
                f"❌ {target.mention} does not have any KSP level roles unlocked.",
                ephemeral=True
            )
            return
            
        view = ModLevelRemoveView(target, unlocked)
        embed = discord.Embed(
            title="🛠️ Mod Role Removal",
            description=f"Select the KSP levels to remove from {target.mention}.\nYou can select specific levels, or choose **Remove ALL Levels**.",
            color=discord.Color.red()
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
    
    bot.add_view(GenericRoleView())


async def check_and_award_level(bot: commands.Bot, guild_id: int, user_id: int, level: int):
    """Check if user achieved a new level, update store and DM them if so."""
    if level not in settings.LEVEL_ROLES:
        return
        
    is_new = await store.add_unlocked_level(guild_id, user_id, level)
    
    # Check if we need to sync from their existing roles just to be sure
    unlocked = await sync_user_levels(bot, user_id)
    
    if is_new or level not in unlocked:
        unlocked.add(level)
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if not user:
            return
            
        role_info = settings.LEVEL_ROLES[level]
        title_name = role_info[1]
        desc = role_info[2]
        
        embed = discord.Embed(
            title="🎉 New KSP Achievement Unlocked!",
            description=f"Congratulations! You've achieved **{title_name}** (`{desc}`).\n\nYou can now equip this title in the server using the menu below. You can display multiple titles at once!",
            color=discord.Color.gold()
        )
        
        equipped = get_equipped_levels(bot, user_id)
        view = LevelRoleView(unlocked, equipped)
        
        try:
            await user.send(embed=embed, view=view)
            log.info("Sent level %d upgrade DM to user %d", level, user_id)
        except discord.Forbidden:
            log.warning("Could not send level upgrade DM to user %d (Forbidden)", user_id)
