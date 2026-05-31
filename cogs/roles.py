import logging
import discord
from discord.ext import commands
from discord.ui import View, Button

import settings
from data.store import store

log = logging.getLogger(__name__)

class LevelRoleView(View):
    def __init__(self, level: int):
        super().__init__(timeout=None)
        self.level = level
        
        # Custom ID includes the level so we can reconstruct it
        self.btn = Button(
            label=f"Equip Level {level} Title",
            style=discord.ButtonStyle.primary,
            custom_id=f"level_role_btn:{level}"
        )
        self.btn.callback = self.toggle_role
        self.add_item(self.btn)

    async def toggle_role(self, interaction: discord.Interaction):
        uid = interaction.user.id
        
        # Find user's max unlocked level across all guilds
        max_unlocked = 0
        for guild in interaction.client.guilds:
            user_data = store.get_user(guild.id, uid)
            if user_data.get("max_unlocked_level", 0) > max_unlocked:
                max_unlocked = user_data["max_unlocked_level"]
                
        if max_unlocked < self.level:
            await interaction.response.send_message("❌ You have not unlocked this level.", ephemeral=True)
            return

        role_info = settings.LEVEL_ROLES.get(self.level)
        if not role_info:
            await interaction.response.send_message("❌ Role configuration not found.", ephemeral=True)
            return
            
        role_id = role_info[0]
        title_name = role_info[1]
        
        added = False
        removed = False
        
        # Apply changes across all guilds
        for guild in interaction.client.guilds:
            member = guild.get_member(uid)
            if not member:
                continue
                
            has_role = any(r.id == role_id for r in member.roles)
            
            if has_role:
                # Remove it
                role = guild.get_role(role_id)
                if role:
                    try:
                        await member.remove_roles(role, reason="User unequipped level role via DM")
                        removed = True
                    except discord.Forbidden:
                        log.warning("Missing permissions to remove role %s from %s in %s", role_id, uid, guild.id)
            else:
                # Add it, and remove other level roles
                roles_to_add = []
                roles_to_remove = []
                
                target_role = guild.get_role(role_id)
                if target_role:
                    roles_to_add.append(target_role)
                    
                for lvl, r_info in settings.LEVEL_ROLES.items():
                    if lvl != self.level:
                        r = guild.get_role(r_info[0])
                        if r and r in member.roles:
                            roles_to_remove.append(r)
                            
                try:
                    if roles_to_remove:
                        await member.remove_roles(*roles_to_remove, reason="User equipped a new level role")
                    if roles_to_add:
                        await member.add_roles(*roles_to_add, reason="User equipped level role via DM")
                    added = True
                except discord.Forbidden:
                    log.warning("Missing permissions to manage roles for %s in %s", uid, guild.id)
                
        if added:
            self.btn.label = f"Unequip {title_name}"
            self.btn.style = discord.ButtonStyle.secondary
            try:
                await interaction.response.edit_message(content=f"✅ You have equipped the **{title_name}** role!", view=self)
            except discord.InteractionResponded:
                pass
        elif removed:
            self.btn.label = f"Equip {title_name}"
            self.btn.style = discord.ButtonStyle.primary
            try:
                await interaction.response.edit_message(content=f"✅ You have unequipped the **{title_name}** role.", view=self)
            except discord.InteractionResponded:
                pass
        else:
            await interaction.response.send_message("⚠️ Could not find you in any server to update roles, or missing permissions.", ephemeral=True)


class Roles(commands.Cog, name="Roles"):
    """Role management and level titles."""
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
    
    # Register all possible level views for persistence
    for level in settings.LEVEL_ROLES.keys():
        bot.add_view(LevelRoleView(level))


async def check_and_award_level(bot: commands.Bot, guild_id: int, user_id: int, level: int):
    """Check if user achieved a new high level, update store and DM them if so."""
    if level not in settings.LEVEL_ROLES:
        return
        
    is_new = await store.update_max_unlocked_level(guild_id, user_id, level)
    if is_new:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if not user:
            return
            
        role_info = settings.LEVEL_ROLES[level]
        title_name = role_info[1]
        desc = role_info[2]
        
        embed = discord.Embed(
            title="🎉 New KSP Achievement Unlocked!",
            description=f"Congratulations! You've achieved **{title_name}** (`{desc}`).\n\nYou can now equip this title in the server. Equipping it will replace any previous level titles you have.",
            color=discord.Color.gold()
        )
        
        view = LevelRoleView(level)
        try:
            await user.send(embed=embed, view=view)
            log.info("Sent level %d upgrade DM to user %d", level, user_id)
        except discord.Forbidden:
            log.warning("Could not send level upgrade DM to user %d (Forbidden)", user_id)
