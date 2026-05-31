import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select

import settings
from data.store import store
from i18n import tp

log = logging.getLogger(__name__)

async def sync_user_max_level(bot: commands.Bot, uid: int) -> int:
    """Scans all guilds to find the highest level role the user has.
    Updates the store if it's higher than current, and returns the max unlocked level.
    """
    max_found = 0
    
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
            
        for level, r_info in settings.LEVEL_ROLES.items():
            role_id = r_info[0]
            if any(r.id == role_id for r in member.roles):
                if level > max_found:
                    max_found = level
                    
    # Ensure store is updated if we found a higher level
    if max_found > 0:
        # We need a guild_id for store. Let's just use the first guild we share, or a generic one.
        # Since max_unlocked_level is checked across all guilds later, updating it in one is fine.
        shared_guilds = [g.id for g in bot.guilds if g.get_member(uid)]
        if shared_guilds:
            await store.update_max_unlocked_level(shared_guilds[0], uid, max_found)
            
    # Also check what's already in the store just in case they unlocked it but don't have it equipped
    store_max = 0
    for g in bot.guilds:
        user_data = store.get_user(g.id, uid)
        s_lvl = user_data.get("max_unlocked_level", 0)
        if s_lvl > store_max:
            store_max = s_lvl
            
    return max(max_found, store_max)


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
    def __init__(self, max_level: int, equipped: set[int]):
        self.max_level = max_level
        options = []
        for lvl in range(1, max_level + 1):
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
        max_unlocked = await sync_user_max_level(interaction.client, uid)
        if any(lvl > max_unlocked for lvl in selected_levels):
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
    def __init__(self, max_level: int, equipped: set[int]):
        super().__init__(timeout=None)
        self.add_item(LevelSelector(max_level, equipped))


class GenericRoleView(View):
    """A generic persistent view to catch old dropdown interactions."""
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.select(custom_id="level_role_dropdown", options=[discord.SelectOption(label="loading", value="0")])
    async def fallback_callback(self, interaction: discord.Interaction, select: Select):
        # If someone clicks an old dropdown, we just generate a fresh one for them via ephemeral message
        # Since Discord handles multi-select state on the client, we actually receive the selected values!
        # But to be safe and accurate with options, let's just ask them to run /gk roles or process it dynamically
        uid = interaction.user.id
        max_unlocked = await sync_user_max_level(interaction.client, uid)
        equipped = get_equipped_levels(interaction.client, uid)
        
        # We can dynamically recreate the proper selector and delegate
        proper_selector = LevelSelector(max_unlocked, equipped)
        proper_selector.values = select.values # Transfer the user's selection
        await proper_selector.callback(interaction)


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
        
        # Determine max level and equipped levels
        max_unlocked = await sync_user_max_level(self.bot, uid)
        if max_unlocked == 0:
            await interaction.response.send_message(
                "❌ You have not unlocked any KSP titles yet. Complete missions or upload screenshots to earn them!",
                ephemeral=True
            )
            return
            
        equipped = get_equipped_levels(self.bot, uid)
        view = LevelRoleView(max_unlocked, equipped)
        
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
    
    # Register generic view for persistence so old dropdowns don't crash
    bot.add_view(GenericRoleView())


async def check_and_award_level(bot: commands.Bot, guild_id: int, user_id: int, level: int):
    """Check if user achieved a new high level, update store and DM them if so."""
    if level not in settings.LEVEL_ROLES:
        return
        
    is_new = await store.update_max_unlocked_level(guild_id, user_id, level)
    
    # We also check if we need to sync from their existing roles just to be sure
    max_unlocked = await sync_user_max_level(bot, user_id)
    
    if is_new or level > max_unlocked:
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
        # Assuming the new max is 'level' or higher
        view = LevelRoleView(max(level, max_unlocked), equipped)
        
        try:
            await user.send(embed=embed, view=view)
            log.info("Sent level %d upgrade DM to user %d", level, user_id)
        except discord.Forbidden:
            log.warning("Could not send level upgrade DM to user %d (Forbidden)", user_id)
