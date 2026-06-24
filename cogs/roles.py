import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select

import settings
from data import achievements
from data import guild_config
from i18n import tp, t, S
from cogs.moderation import mod_only

log = logging.getLogger(__name__)

# ── Extra translation strings (notifications + self-disable) ─────────────────
S.update({
    "roles.not_configured": {"en": "❌ KSP titles aren't set up on **{guild}** yet. Ask an admin to map the achievement roles with `/admin setrole`."},
    "roles.notif_enabled":  {"en": "🔔 Notifications enabled in **{count}** server(s). You'll be pinged for weekly missions & announcements."},
    "roles.notif_disabled": {"en": "🔕 Notifications turned off in **{count}** server(s)."},
    "roles.notif_none":     {"en": "⚠️ None of the servers you share with me have a notification role set up yet."},
})


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers — achievements are GLOBAL per user; role IDs are PER GUILD.
# ═══════════════════════════════════════════════════════════════════════════

async def sync_user_levels(bot: commands.Bot, uid: int) -> set[int]:
    """Fold any level roles the user already wears (in any guild, resolved via that
    guild's mapped role IDs) into the global achievement store, then return the
    user's full global unlocked set."""
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
        for level in settings.LEVEL_ROLES:
            role_id = guild_config.get_role_id(guild.id, f"level_{level}")
            if role_id and any(r.id == role_id for r in member.roles):
                achievements.add_unlocked(uid, level)
    return achievements.get_unlocked(uid)


def get_equipped_levels(bot: commands.Bot, uid: int) -> set[int]:
    """Levels the user currently has equipped (the role applied) in any guild."""
    equipped = set()
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
        for level in settings.LEVEL_ROLES:
            role_id = guild_config.get_role_id(guild.id, f"level_{level}")
            if role_id and any(r.id == role_id for r in member.roles):
                equipped.add(level)
    return equipped


async def _set_notifications(bot: commands.Bot, uid: int, enable: bool) -> tuple[int, int]:
    """Add/remove the mapped notification role for the user across every guild that
    has one configured. Returns (changed, available)."""
    changed = available = 0
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if not member:
            continue
        role = guild_config.resolve_role(guild, "notifications")
        if role is None:
            continue
        available += 1
        has = role in member.roles
        try:
            if enable and not has:
                await member.add_roles(role, reason="User opted into notifications")
                changed += 1
            elif not enable and has:
                await member.remove_roles(role, reason="User opted out of notifications")
                changed += 1
        except discord.Forbidden:
            log.warning("Missing permission to toggle notification role for %s in %s", uid, guild.id)
    return changed, available


async def _handle_notif(interaction: discord.Interaction, enable: bool) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    uid = interaction.user.id
    gid = interaction.guild_id
    changed, available = await _set_notifications(interaction.client, uid, enable)
    if available == 0:
        await interaction.followup.send(tp(gid, uid, "roles.notif_none"), ephemeral=True)
        return
    key = "roles.notif_enabled" if enable else "roles.notif_disabled"
    await interaction.followup.send(tp(gid, uid, key, count=changed), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Level title selector (DM)
# ═══════════════════════════════════════════════════════════════════════════

class LevelSelector(Select):
    def __init__(self, unlocked: set[int], equipped: set[int], guild_id: int | None, user_id: int):
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
            options.append(discord.SelectOption(
                label=tp(guild_id, user_id, "roles.none_unlocked"),
                value="0"
            ))

        super().__init__(
            placeholder=tp(guild_id, user_id, "roles.select_placeholder"),
            min_values=0,
            max_values=len(options) if options[0].value != "0" else 1,
            options=options,
            custom_id="level_role_dropdown"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        uid = interaction.user.id
        gid = interaction.guild_id

        if "0" in self.values and len(self.values) == 1:
            await interaction.followup.send(tp(gid, uid, "roles.no_titles"), ephemeral=True)
            return

        selected_levels = set(int(v) for v in self.values)

        # Verify they aren't cheating the client against their GLOBAL achievements.
        unlocked = await sync_user_levels(interaction.client, uid)
        if any(lvl not in unlocked for lvl in selected_levels):
            await interaction.followup.send(tp(gid, uid, "roles.invalid_selection"), ephemeral=True)
            return

        added_count = 0
        removed_count = 0

        # Apply across every guild where the role feature is fully configured,
        # using each guild's own mapped role IDs.
        for guild in interaction.client.guilds:
            member = guild.get_member(uid)
            if not member or not guild_config.roles_ready(guild):
                continue

            roles_to_add = []
            roles_to_remove = []

            for lvl in settings.LEVEL_ROLES:
                role_id = guild_config.get_role_id(guild.id, f"level_{lvl}")
                role_obj = guild.get_role(role_id) if role_id else None
                if not role_obj:
                    continue

                has_role = any(r.id == role_obj.id for r in member.roles)

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

        await interaction.followup.send(
            tp(gid, uid, "roles.updated", count=len(selected_levels)),
            ephemeral=True
        )


class LevelRoleView(View):
    def __init__(self, unlocked: set[int], equipped: set[int], guild_id: int | None, user_id: int):
        super().__init__(timeout=None)
        self.add_item(LevelSelector(unlocked, equipped, guild_id, user_id))

    @discord.ui.button(label="🔔 Enable notifications",
                       style=discord.ButtonStyle.green, custom_id="gk_notif_on", row=1)
    async def notif_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_notif(interaction, True)

    @discord.ui.button(label="🔕 Disable notifications",
                       style=discord.ButtonStyle.grey, custom_id="gk_notif_off", row=1)
    async def notif_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_notif(interaction, False)


class GenericRoleView(View):
    """A generic persistent view to catch old dropdown / notification interactions
    after a restart (the live LevelRoleView handles them while it's still active)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(custom_id="level_role_dropdown", options=[discord.SelectOption(label="loading", value="0")])
    async def fallback_callback(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        gid = interaction.guild_id
        unlocked = await sync_user_levels(interaction.client, uid)
        equipped = get_equipped_levels(interaction.client, uid)

        proper_selector = LevelSelector(unlocked, equipped, gid, uid)
        proper_selector.values = select.values
        await proper_selector.callback(interaction)

    @discord.ui.button(label="🔔 Enable notifications",
                       style=discord.ButtonStyle.green, custom_id="gk_notif_on", row=1)
    async def notif_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_notif(interaction, True)

    @discord.ui.button(label="🔕 Disable notifications",
                       style=discord.ButtonStyle.grey, custom_id="gk_notif_off", row=1)
    async def notif_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_notif(interaction, False)


# ═══════════════════════════════════════════════════════════════════════════
#  Mod-side level removal
# ═══════════════════════════════════════════════════════════════════════════

class ModLevelRemoveSelector(Select):
    def __init__(self, target: discord.Member, unlocked: set[int], guild_id: int, user_id: int):
        self.target = target
        options = [discord.SelectOption(
            label=tp(guild_id, user_id, "roles.mod_remove_all"),
            value="0",
            description=tp(guild_id, user_id, "roles.mod_remove_all_desc")
        )]

        for lvl in sorted(list(unlocked)):
            if lvl in settings.LEVEL_ROLES:
                r_info = settings.LEVEL_ROLES[lvl]
                options.append(discord.SelectOption(
                    label=tp(guild_id, user_id, "roles.mod_level_name", lvl=lvl, name=r_info[1]),
                    value=str(lvl)
                ))

        super().__init__(
            placeholder=tp(guild_id, user_id, "roles.mod_select_placeholder", name=target.display_name),
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected = set(int(v) for v in self.values)
        guild = interaction.guild
        uid = self.target.id
        mod_uid = interaction.user.id
        gid = interaction.guild_id

        # Check if they selected 0 (Remove ALL)
        remove_all = 0 in selected

        removed_roles = []
        if remove_all:
            # Clear the user's GLOBAL achievements and strip this guild's level roles.
            achievements.remove_unlocked(uid, 0)
            for lvl in settings.LEVEL_ROLES:
                role_id = guild_config.get_role_id(guild.id, f"level_{lvl}")
                role = guild.get_role(role_id) if role_id else None
                if role and role in self.target.roles:
                    removed_roles.append(role)
        else:
            for lvl in selected:
                achievements.remove_unlocked(uid, lvl)
                role_id = guild_config.get_role_id(guild.id, f"level_{lvl}")
                role = guild.get_role(role_id) if role_id else None
                if role and role in self.target.roles:
                    removed_roles.append(role)

        if removed_roles:
            try:
                await self.target.remove_roles(*removed_roles, reason=f"Mod {interaction.user} removed level roles")
            except discord.Forbidden:
                await interaction.followup.send(tp(gid, mod_uid, "roles.mod_no_perms"), ephemeral=True)
                return

        # Disable select
        self.disabled = True
        await interaction.edit_original_response(view=self.view)

        await interaction.followup.send(
            tp(gid, mod_uid, "roles.mod_success", count='ALL' if remove_all else len(selected), user=self.target.mention),
            ephemeral=True
        )


class ModLevelRemoveView(View):
    def __init__(self, target: discord.Member, unlocked: set[int], guild_id: int, user_id: int):
        super().__init__(timeout=300)
        self.add_item(ModLevelRemoveSelector(target, unlocked, guild_id, user_id))


# ═══════════════════════════════════════════════════════════════════════════
#  Cog
# ═══════════════════════════════════════════════════════════════════════════

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
        gid = interaction.guild_id

        # Self-disable: in a server where the achievement roles aren't fully
        # mapped, the feature is unavailable (achievements are still tracked).
        if interaction.guild is not None and not guild_config.roles_ready(interaction.guild):
            await interaction.response.send_message(
                tp(gid, uid, "roles.not_configured", guild=interaction.guild.name),
                ephemeral=True
            )
            return

        unlocked = await sync_user_levels(self.bot, uid)
        if not unlocked:
            await interaction.response.send_message(
                tp(gid, uid, "roles.cmd_no_unlocked"),
                ephemeral=True
            )
            return

        equipped = get_equipped_levels(self.bot, uid)
        view = LevelRoleView(unlocked, equipped, gid, uid)

        embed = discord.Embed(
            title=tp(gid, uid, "roles.embed_title"),
            description=tp(gid, uid, "roles.embed_desc"),
            color=discord.Color.blue()
        )

        try:
            await interaction.user.send(embed=embed, view=view)
            await interaction.response.send_message(
                tp(gid, uid, "roles.check_dm"),
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                tp(gid, uid, "roles.no_dm"),
                ephemeral=True
            )

    @app_commands.command(
        name="removeroles",
        description="[MOD] Remove KSP level roles from a user"
    )
    @app_commands.describe(target="The user to remove level roles from")
    @app_commands.default_permissions(kick_members=True)
    @mod_only()
    async def removeroles_cmd(self, interaction: discord.Interaction, target: discord.Member) -> None:
        mod_uid = interaction.user.id
        gid = interaction.guild_id

        unlocked = await sync_user_levels(self.bot, target.id)
        if not unlocked:
            await interaction.response.send_message(
                tp(gid, mod_uid, "roles.mod_no_unlocked", user=target.mention),
                ephemeral=True
            )
            return

        view = ModLevelRemoveView(target, unlocked, gid, mod_uid)
        embed = discord.Embed(
            title=tp(gid, mod_uid, "roles.mod_embed_title"),
            description=tp(gid, mod_uid, "roles.mod_embed_desc", user=target.mention),
            color=discord.Color.red()
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))

    bot.add_view(GenericRoleView())


async def check_and_award_level(bot: commands.Bot, guild_id: int, user_id: int, level: int) -> bool:
    """Record a newly-earned KSP level (globally) and DM the user a title selector.

    Returns True when this is a NEW unlock (the user didn't already have the level),
    False when it was already earned or the level is invalid. Callers that fire this
    as a background task can ignore the result."""
    if level not in settings.LEVEL_ROLES:
        return False

    is_new = achievements.add_unlocked(user_id, level)
    if not is_new:
        return False  # already earned — nothing to announce

    unlocked = await sync_user_levels(bot, user_id)
    unlocked.add(level)

    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    if not user:
        return

    role_info = settings.LEVEL_ROLES[level]
    title_name = role_info[1]
    desc = role_info[2]

    # We use user_id to respect their personal language setting!
    embed = discord.Embed(
        title=tp(guild_id, user_id, "roles.unlocked_title"),
        description=tp(guild_id, user_id, "roles.unlocked_desc", title_name=title_name, desc=desc),
        color=discord.Color.gold()
    )

    equipped = get_equipped_levels(bot, user_id)
    view = LevelRoleView(unlocked, equipped, guild_id, user_id)

    try:
        await user.send(embed=embed, view=view)
        log.info("Sent level %d upgrade DM to user %d", level, user_id)
    except discord.Forbidden:
        log.warning("Could not send level upgrade DM to user %d (Forbidden)", user_id)

    return True
