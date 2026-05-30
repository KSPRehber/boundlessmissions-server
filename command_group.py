"""
command_group.py – Shared slash command group.

If COMMAND_GROUP is set in .env (e.g. "gk"), all slash commands will be
registered under /gk <command>.  If it's empty, this module exports None
and cogs should use plain @app_commands.command decorators instead.
"""

from discord import app_commands
from config import cfg

# When COMMAND_GROUP is set, create a single shared Group instance.
# Every cog imports this and adds its commands to it.
if cfg.COMMAND_GROUP:
    gk = app_commands.Group(
        name=cfg.COMMAND_GROUP.lower(),
        description="Gene Kerman bot commands",
    )
else:
    gk = None
