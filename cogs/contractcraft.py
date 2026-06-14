"""
cogs/contractcraft.py – "Load to KSP" button on corp-delivered contract crafts.

When a bot contract is completed, the finished craft is posted to the builder's
corporation channel (see api_server._deliver_craft_to_corp). This adds a button to
that message so a corp member can queue the craft for their KSP client to install
as a blueprint (NOT a live vessel — bot-contract vessels are never spawned, so they
can't be re-imported/duplicated). The KSP mod polls the queue and installs it.

The builder can't import their own craft. Button is a DynamicItem so it keeps
working after bot restarts.
"""
import logging

import discord
from discord.ext import commands
from discord.ui import View, Button, DynamicItem

from data import contracts as cdb
from data import imports as imp

log = logging.getLogger(__name__)

# contract_ids are 12-char hex, guild_ids are snowflakes
_ID_PATTERN = r"(?P<cid>[^:]+):(?P<gid>\d+)"


def _craft_name(c: dict, filename: str) -> str:
    return (c.get("vessel_data") or {}).get("vessel_name") or filename[:-6]


class CorpLoadButton(DynamicItem[Button], template=r"cc_load:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="🚀 Load to KSP", style=discord.ButtonStyle.blurple,
                                custom_id=f"cc_load:{contract_id}:{guild_id}"))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            await interaction.followup.send("❌ This craft is no longer available.", ephemeral=True)
            return

        craft_files = [f for f in c.get("submitted_files", []) if f.get("filename", "").endswith(".craft")]
        if not craft_files:
            await interaction.followup.send("❌ No craft file on this contract.", ephemeral=True)
            return

        # No self-imports — the builder already has their own work.
        if str(interaction.user.id) == str(c.get("contractor_id")):
            await interaction.followup.send(
                "❌ You built this craft — you can't import your own work.", ephemeral=True)
            return

        cf = craft_files[0]
        craft_name = _craft_name(c, cf["filename"])
        imp.enqueue(
            self.gid, interaction.user.id, "contract", self.cid, craft_name,
            craft_url=cf["url"], craft_filename=cf["filename"],
        )
        await interaction.followup.send(
            f"✅ **{craft_name}** is queued. Open KSP and it'll auto-install to your Ships "
            "folder at the Space Center.",
            ephemeral=True,
        )
        log.info("CorpCraft: %s queued contract craft %s", interaction.user, self.cid)


class CorpCraftView(View):
    """Persistent view attached to the corp delivery message."""
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(CorpLoadButton(contract_id, guild_id))


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(CorpLoadButton)
