"""Slash-command gates: which moderator commands rely on Discord's
`default_permissions` alone (a per-guild *default* any server admin can widen to
@everyone in Integrations), the /setxp value bound, and the mimic exclusion."""
import signal

from _h import check, section, finish, src

import discord

section("commands whose only gate is default_permissions")
from cogs import contracts, corps, tickets, economy, xp, admin, weeklymissions, roles, gkchannels

def cmd(cog_cls, name):
    c = getattr(cog_cls, name)
    return c if isinstance(c, discord.app_commands.Command) else c

for cog_cls, name, effect in (
        (contracts.Contracts, "contractreset", "cancels every contract of any account, refunds escrow, restores wrecks"),
        (corps.Corps, "corpsgenerate", "creates a channel per linked member"),
        (corps.Corps, "corpsprivacy", "rewrites permission overwrites on every corp channel"),
        (tickets.Tickets, "ticketpanel", "posts a ticket panel")):
    c = cmd(cog_cls, name)
    check(f"/{name} has an in-code authority check ({effect})", bool(c.checks),
          f"checks={c.checks}; only @default_permissions on the command")

for cog_cls, name in ((economy.Economy, "setbalance"), (economy.Economy, "fine"),
                      (economy.Economy, "givemoney"), (xp.XP, "setxp"), (roles.Roles, "removeroles_cmd"),
                      (gkchannels.GKChannels, "gkchannel"), (admin.Admin, "mimic"), (admin.Admin, "linkas"),
                      (admin.Admin, "publishversion"), (admin.Admin, "policyversion")):
    check(f"control: /{cmd(cog_cls, name).name} carries a runtime check", bool(cmd(cog_cls, name).checks))
wm = src("cogs/weeklymissions.py")
check("control: /add_custom_mission checks the real invoker in its body",
      "ru.guild_permissions.kick_members" in wm)

section("/setxp and admin xp_set: value bound")
from data.store import level_from_xp

def _alarm(*_): raise TimeoutError
signal.signal(signal.SIGALRM, _alarm)
signal.alarm(3)
try:
    level_from_xp(2 ** 53)          # the largest integer a Discord option can carry
    hung = False
except TimeoutError:
    hung = True
finally:
    signal.alarm(0)
check("level_from_xp(2**53) returns in under 3 s", not hung,
      "loops ~2e9 times (exponent 1.5); store.set_xp holds store._lock on the event loop "
      "the whole time — /setxp amount:9007199254740991 (guild administrator) or the owner "
      "console's xp_set stalls the bot")

section("mimic")
check("Interaction.command is resolved lazily (so the mimic/unmimic exclusion sees a name)",
      isinstance(discord.Interaction.__dict__.get("command"), property)
      or hasattr(discord.Interaction.command, "__get__"))
b = src("bot.py")
check("mimic map is keyed by the REAL invoker's id",
      "real_id = getattr(real_user, \"id\", None)" in b and "mmap.get(real_id)" in b)
a = src("cogs/admin.py")
check("/mimic and /unmimic are owner-gated", a.count("@is_owner()\n    async def mimic") == 1
      and "@is_owner()\n    async def unmimic" in a)
check("/linkas is owner-gated", "@is_owner()\n    async def linkas" in a)

section("KSP link identity")
kb = src("cogs/ksp_bridge.py")
body = kb[kb.index("async def linkcode"):kb.index("embed =", kb.index("async def linkcode"))]
check("/linkcode mints the code for the account the Discord id resolves to",
      "account_for_discord" in body,
      "uses interaction.user.id verbatim: a Discord user joined onto a web account (join_accounts kept "
      "the web side) gets a KSP token for the orphan snowflake — its wallet is not the account's, and a "
      "console suspension keyed on the account id does not cover that token")
finish()
