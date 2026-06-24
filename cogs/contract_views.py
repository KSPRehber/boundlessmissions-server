"""Contract interactive button views.

All buttons use DynamicItem with regex-matched custom_ids so they
survive bot restarts. Contract/guild info is encoded in the custom_id:
"prefix:contract_id:guild_id"
"""
import logging
import discord
from discord.ui import View, Button, DynamicItem, button
from i18n import t, tp
import settings
from data.store import store
from data import contracts as cdb
from data import imports as imp
from data import guild_config

log = logging.getLogger(__name__)

# ── Regex pattern reused by all buttons ──────────────────────────────────────
# contract_ids are Firestore auto-IDs (alphanumeric), guild_ids are snowflakes
_ID_PATTERN = r"(?P<cid>[^:]+):(?P<gid>\d+)"


def _cid(prefix: str, contract_id: str, guild_id: int) -> str:
    return f"{prefix}:{contract_id}:{guild_id}"


def _embed(c, guild_id):
    is_flag = c.get("mission_type") == cdb.FLAG_DESIGN
    title = f"🚩 {t(guild_id, 'ct.title')}" if is_flag else f"📜 {t(guild_id, 'ct.title')}"
    e = discord.Embed(title=title, color=discord.Color.gold())
    sym = settings.CURRENCY_SYMBOL
    e.add_field(name=t(guild_id, "ct.mission"), value=c["mission"], inline=False)
    e.add_field(name=t(guild_id, "ct.issuer"), value=c["issuer_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.contractor"), value=c["contractor_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.payment"), value=f"**{c['payment']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.fine"), value=f"**{c['fine']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.due"), value=c["due_date"], inline=True)
    e.add_field(name=t(guild_id, "ct.status"), value=f"`{c['status']}`", inline=True)
    
    if c.get("modlist"):
        # Truncate if necessary to fit in Discord's 1024 char limit for fields
        mod_text = c["modlist"]
        if len(mod_text) > 1000:
            mod_text = mod_text[:1000] + "..."
        e.add_field(name="Required Mods", value=f"```\n{mod_text}\n```", inline=False)

    # Flag-design contracts ride the watermarked preview along on every embed.
    # The clean full-res image stays gated until the contract completes.
    if is_flag and c.get("flag_preview_url"):
        e.set_image(url=c["flag_preview_url"])

    return e


# ══════════════════════════════════════════════════════════════════════════════
#  DynamicItem Button Classes
# ══════════════════════════════════════════════════════════════════════════════

# ── Offer View Buttons (Accept / Refuse) ─────────────────────────────────────

class AcceptOfferButton(DynamicItem[Button], template=r"ct_accept:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_accept", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.PENDING:
            await interaction.followup.send("❌", ephemeral=True)
            return
        cdb.update_contract(self.gid, self.cid, status=cdb.ACTIVE)
        c["status"] = cdb.ACTIVE
        e = _embed(c, self.gid)
        e.color = discord.Color.green()
        await interaction.edit_original_response(embed=e, view=ContractWorkView(self.cid, self.gid))


class RefuseOfferButton(DynamicItem[Button], template=r"ct_refuse:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_refuse", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        cdb.update_contract(self.gid, self.cid, status=cdb.CANCELLED)
        await store.add_balance(self.gid, int(c["issuer_id"]), c["payment"])
        c["status"] = cdb.CANCELLED
        e = _embed(c, self.gid)
        e.color = discord.Color.red()
        await interaction.edit_original_response(embed=e, view=None)


# ── Work View Buttons (Give Up / Submit) ─────────────────────────────────────

class GiveUpButton(DynamicItem[Button], template=r"ct_giveup:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="🏳️ Give Up", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_giveup", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.ACTIVE:
            return
        cdb.update_contract(self.gid, self.cid, status=cdb.CANCELLED)
        await store.add_balance(self.gid, int(c["issuer_id"]), c["payment"])
        c["status"] = cdb.CANCELLED
        e = _embed(c, self.gid)
        e.color = discord.Color.red()
        await interaction.edit_original_response(embed=e, view=None)


class SubmitButton(DynamicItem[Button], template=r"ct_submit:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="📤 Submit", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("ct_submit", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.ACTIVE:
            return
        # Get the real user ID in case an admin is mimicking someone
        real_user = getattr(interaction, "extras", {}).get("_mimic_real_user", interaction.user)
        real_user_id = real_user.id if real_user else interaction.user.id

        # Scan channel backwards for recent files, stopping at contract msg
        files_found = []
        dm_msg_id = int(c.get("dm_message_id") or 0)
        async for msg in interaction.channel.history(limit=50):
            # Stop scanning if we hit the contract message
            if dm_msg_id and msg.id <= dm_msg_id:
                break
            
            if msg.author.id in (interaction.user.id, real_user_id):
                for att in reversed(msg.attachments):
                    files_found.append({"url": att.url, "filename": att.filename,
                                        "content_type": att.content_type or "application/octet-stream"})
        # Reverse so order is chronological
        files_found.reverse()
        if not files_found:
            await interaction.followup.send("❌ No files found. Upload files here first.", ephemeral=True)
            return
        # Require at least one image (screenshot)
        has_image = any(f["content_type"].startswith("image/") for f in files_found)
        if not has_image:
            await interaction.followup.send(
                "❌ Missing screenshot (image). Upload at least a screenshot.",
                ephemeral=True)
            return
        view = FileSelectView(self.cid, self.gid, files_found)
        await interaction.followup.send(embed=view._generate_embed(), view=view, ephemeral=True)


# ── Review View Buttons (Issuer accepts/refuses submission) ──────────────────

class ReviewAcceptButton(DynamicItem[Button], template=r"ct_rv_acc:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_rv_acc", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.SUBMITTED:
            return
        if str(interaction.user.id) != str(c.get("issuer_id")):
            await interaction.followup.send("❌ Only the contract issuer can review submissions.", ephemeral=True)
            return
        from datetime import datetime
        cdb.update_contract(self.gid, self.cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        await store.add_balance(self.gid, int(c["contractor_id"]), c["payment"])
        # Credit the rescuer with a completed rescue for the leaderboard/stats.
        if c.get("mission_type") == cdb.RESCUE:
            await store.add_rescue(self.gid, int(c["contractor_id"]))
        c["status"] = cdb.COMPLETED
        # Flag-design: deliver the full-res flag to the issuer's in-game picker.
        if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
            imp.enqueue(self.gid, int(c["issuer_id"]), source="flag", ref_id=self.cid,
                        craft_name=c["mission"], flag_url=c["flag_fullres_url"],
                        craft_filename=c.get("flag_filename") or "flag.png")
        e = _embed(c, self.gid)
        e.color = discord.Color.green()
        # Reveal craft files (screenshots were already visible)
        files = c.get("submitted_files", [])
        craft_files = [s for s in files if s['filename'].lower().endswith('.craft')]
        if craft_files:
            flist = "\n".join(f"🚀 [{s['filename']}]({s['url']})" for s in craft_files)
            e.add_field(name="📁 Craft Files", value=flist, inline=False)
        screenshots = [s for s in files if not s['filename'].lower().endswith('.craft')]
        if screenshots:
            flist = "\n".join(f"🖼️ [{s['filename']}]({s['url']})" for s in screenshots)
            e.add_field(name="🖼️ Screenshots", value=flist, inline=False)
        # Flag-design: reveal the clean full-res flag now that it's paid for.
        if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
            e.set_image(url=c["flag_fullres_url"])
            e.add_field(name="🚩 Flag (full-res)",
                        value=f"[Download]({c['flag_fullres_url']}); also queued to your "
                              "in-game flag picker.", inline=False)
        await interaction.edit_original_response(embed=e, view=None)
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            ne = discord.Embed(title=f"✅ {t(self.gid, 'ct.accepted')}",
                               description=t(self.gid, 'ct.accepted_desc', payment=c['payment'], sym=settings.CURRENCY_SYMBOL),
                               color=discord.Color.green())
            await contractor.send(embed=ne)
        except Exception:
            pass


class ReviewRefuseButton(DynamicItem[Button], template=r"ct_rv_ref:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_rv_ref", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.SUBMITTED:
            return
        if str(interaction.user.id) != str(c.get("issuer_id")):
            await interaction.followup.send("❌ Only the contract issuer can review submissions.", ephemeral=True)
            return
        cdb.update_contract(self.gid, self.cid, status=cdb.DISPUTED)
        c["status"] = cdb.DISPUTED
        e = _embed(c, self.gid)
        e.color = discord.Color.red()
        await interaction.edit_original_response(embed=e, view=None)
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            de = discord.Embed(title=f"⚠️ {t(self.gid, 'ct.disputed')}",
                               description=t(self.gid, 'ct.disputed_desc'), color=discord.Color.orange())
            await contractor.send(embed=de, view=DisputeView(self.cid, self.gid))
        except Exception:
            pass


# ── Dispute View Buttons (Settle / More Time / Pay Fine / Sue) ───────────────

class SettleButton(DynamicItem[Button], template=r"ct_settle:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="🤝 Settle", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_settle", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        # Bot-issued contracts can't be settled
        if str(c["issuer_id"]) == str(interaction.client.user.id):
            await interaction.followup.send("❌ AI contracts cannot be settled.", ephemeral=True)
            return
        try:
            issuer = await interaction.client.fetch_user(int(c["issuer_id"]))
            e = discord.Embed(title=f"🤝 {t(self.gid, 'ct.settle_request')}",
                              description=t(self.gid, 'ct.settle_desc', name=c['contractor_name']),
                              color=discord.Color.light_grey())
            await issuer.send(embed=e, view=SettleApprovalView(self.cid, self.gid))
            await interaction.followup.send(t(self.gid, "ct.settle_sent"), ephemeral=True)
        except Exception:
            await interaction.followup.send("❌", ephemeral=True)


class MoreTimeButton(DynamicItem[Button], template=r"ct_moretime:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="⏰ More Time", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_moretime", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        c = cdb.get_contract(self.gid, self.cid)
        # Bot-issued: auto-extend to end of week, no modal
        if c and str(c["issuer_id"]) == str(interaction.client.user.id):
            await interaction.response.defer()
            from datetime import datetime, timedelta, timezone
            tz = timezone(timedelta(hours=3))
            now = datetime.now(tz)
            days_to_sunday = 6 - now.weekday()
            if days_to_sunday <= 0:
                days_to_sunday = 7
            end_of_week = (now + timedelta(days=days_to_sunday)).strftime("%Y-%m-%d")
            cdb.update_contract(self.gid, self.cid, due_date=end_of_week, status=cdb.ACTIVE)
            c["status"] = cdb.ACTIVE
            c["due_date"] = end_of_week
            e = _embed(c, self.gid)
            v = ContractWorkView(self.cid, self.gid)
            
            try:
                await interaction.edit_original_response(
                    content=f"⏰ Extended to **{end_of_week}**. Submit again!",
                    embed=e, view=v,
                )
            except Exception:
                pass
                
            # Re-show work view on the contract message if it wasn't the one we just edited
            if c.get("dm_message_id") and (not interaction.message or interaction.message.id != int(c["dm_message_id"])):
                try:
                    ch = interaction.channel or await interaction.client.fetch_channel(interaction.channel_id)
                    orig = await ch.fetch_message(int(c["dm_message_id"]))
                    await orig.edit(embed=e, view=v)
                except Exception:
                    pass
            return
        # Normal contract: open date modal
        await interaction.response.send_modal(MoreTimeModal(self.cid, self.gid))


class PayFineButton(DynamicItem[Button], template=r"ct_payfine:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="💰 Pay Fine", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_payfine", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        # Atomic check-and-deduct so a concurrent spend can't slip the fine past a
        # stale balance read.
        if not await store.try_debit(self.gid, int(c["contractor_id"]), c["fine"]):
            await interaction.followup.send(t(self.gid, "ct.no_funds"), ephemeral=True)
            return
        await store.add_balance(self.gid, int(c["issuer_id"]), c["fine"] + c["payment"])
        from datetime import datetime
        cdb.update_contract(self.gid, self.cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        c["status"] = cdb.COMPLETED
        e = _embed(c, self.gid)
        e.color = discord.Color.dark_red()
        e.set_footer(text=t(self.gid, "ct.fine_paid"))
        await interaction.edit_original_response(embed=e, view=None)


class SueButton(DynamicItem[Button], template=r"ct_sue:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="⚖️ Sue", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("ct_sue", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        bot = interaction.client
        cdb.update_contract(self.gid, self.cid, status=cdb.MOD_REVIEW)
        c["status"] = cdb.MOD_REVIEW
        e = _embed(c, self.gid)
        e.title = f"⚖️ {t(self.gid, 'ct.mod_review')}"
        e.color = discord.Color.purple()
        files = c.get("submitted_files", [])
        if files:
            e.add_field(name="📁", value="\n".join(f"📎 [{f['filename']}]({f['url']})" for f in files), inline=False)

        # Prefer a private ticket (both parties + mods); fall back to the shared
        # mod channel if the ticket system isn't configured.
        ticket_channel = None
        if guild_config.get_channel_id(self.gid, "ticket_category"):
            try:
                from cogs.tickets import create_ticket
                guild = interaction.guild or bot.get_guild(self.gid)
                if guild is not None:
                    other_id = (int(c["issuer_id"])
                                if str(interaction.user.id) == str(c.get("contractor_id"))
                                else int(c["contractor_id"]))
                    ticket_channel = await create_ticket(
                        bot, guild,
                        opener_id=interaction.user.id,
                        kind="other",
                        title="Contract dispute (escalated)",
                        description=(f"{interaction.user.mention} escalated contract "
                                     f"`{self.cid}` for moderator review."),
                        color=discord.Color.purple(),
                        extra_user_ids=[other_id],
                        extra_embeds=[e],
                        extra_view=ModReviewView(self.cid, self.gid),
                    )
            except Exception as exc:
                log.warning("Could not open sue ticket for %s: %s", self.cid, exc)

        if ticket_channel is None:
            ch = guild_config.resolve_channel(bot, self.gid, "contract_mod")
            if ch is None:
                await interaction.followup.send("❌ Not configured.", ephemeral=True)
                return
            await ch.send(embed=e, view=ModReviewView(self.cid, self.gid))

        await interaction.edit_original_response(content=t(self.gid, "ct.sued"), view=None)


# ── More Time Approval Buttons ───────────────────────────────────────────────

class MoreTimeApproveButton(DynamicItem[Button], template=r"ct_mt_y:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int, new_date: str = ""):
        super().__init__(Button(label="✅ Approve Extension", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_mt_y", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)
        self.new_date = new_date

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        # Extract new_date from embed description if reloaded after restart
        new_date = self.new_date
        if not new_date:
            if interaction.message and interaction.message.embeds:
                desc = interaction.message.embeds[0].description or ""
                new_date = desc.strip().split()[-1]
        cdb.update_contract(self.gid, self.cid, due_date=new_date, status=cdb.ACTIVE)
        await interaction.edit_original_response(
            content=f"✅ Deadline extended to **{new_date}**. Contract is active again.",
            embed=None, view=None,
        )
        # Notify contractor and give them the work view back
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            c["status"] = cdb.ACTIVE
            c["due_date"] = new_date
            e = _embed(c, self.gid)
            e.color = discord.Color.green()
            await contractor.send(embed=e, view=ContractWorkView(self.cid, self.gid))
        except Exception:
            pass


class MoreTimeRefuseButton(DynamicItem[Button], template=r"ct_mt_n:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_mt_n", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="❌ Extension refused.", embed=None, view=None,
        )
        c = cdb.get_contract(self.gid, self.cid)
        if c:
            try:
                contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
                await contractor.send("❌ Your time extension request was refused.")
            except Exception:
                pass


# ── Settle Approval Buttons ──────────────────────────────────────────────────

class SettleApproveButton(DynamicItem[Button], template=r"ct_stl_y:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept Settlement", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_stl_y", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        await store.add_balance(self.gid, int(c["issuer_id"]), c["payment"])
        cdb.update_contract(self.gid, self.cid, status=cdb.CANCELLED)
        await interaction.edit_original_response(content=f"✅ {t(self.gid, 'ct.settled')}", embed=None, view=None)


class SettleRefuseButton(DynamicItem[Button], template=r"ct_stl_n:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_stl_n", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(content=f"❌ {t(self.gid, 'ct.settle_refused')}", embed=None, view=None)


# ── Mod Review Buttons ───────────────────────────────────────────────────────

class ModEnforceButton(DynamicItem[Button], template=r"ct_mod_f:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Enforce Fine", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_mod_f", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        # Take whatever the contractor can pay toward the fine, atomically, and pass
        # exactly that amount to the issuer (plus the escrowed payment).
        fine = await store.debit_up_to(self.gid, int(c["contractor_id"]), c["fine"])
        if fine > 0:
            await store.add_balance(self.gid, int(c["issuer_id"]), fine)
        await store.add_balance(self.gid, int(c["issuer_id"]), c["payment"])
        from datetime import datetime
        cdb.update_contract(self.gid, self.cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        await interaction.edit_original_response(content=f"✅ Fine enforced ({fine}). Escrow refunded.", view=None)


class ModCancelButton(DynamicItem[Button], template=r"ct_mod_c:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Cancel Fine", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_mod_c", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        await store.add_balance(self.gid, int(c["issuer_id"]), c["payment"])
        cdb.update_contract(self.gid, self.cid, status=cdb.CANCELLED)
        await interaction.edit_original_response(content="❌ Fine cancelled. Escrow refunded.", view=None)


# ══════════════════════════════════════════════════════════════════════════════
#  View Classes (compose DynamicItem instances)
# ══════════════════════════════════════════════════════════════════════════════

class ContractOfferView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(AcceptOfferButton(contract_id, guild_id))
        self.add_item(RefuseOfferButton(contract_id, guild_id))


class ContractWorkView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(GiveUpButton(contract_id, guild_id))
        self.add_item(SubmitButton(contract_id, guild_id))


class ContractReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(ReviewAcceptButton(contract_id, guild_id))
        self.add_item(ReviewRefuseButton(contract_id, guild_id))


class DisputeView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(SettleButton(contract_id, guild_id))
        self.add_item(MoreTimeButton(contract_id, guild_id))
        self.add_item(PayFineButton(contract_id, guild_id))
        self.add_item(SueButton(contract_id, guild_id))


class MoreTimeApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0, new_date: str = ""):
        super().__init__(timeout=None)
        self.add_item(MoreTimeApproveButton(contract_id, guild_id, new_date))
        self.add_item(MoreTimeRefuseButton(contract_id, guild_id))


class SettleApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(SettleApproveButton(contract_id, guild_id))
        self.add_item(SettleRefuseButton(contract_id, guild_id))


class ModReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(ModEnforceButton(contract_id, guild_id))
        self.add_item(ModCancelButton(contract_id, guild_id))


# ══════════════════════════════════════════════════════════════════════════════
#  Non-persistent Views (ephemeral, don't need DynamicItem)
# ══════════════════════════════════════════════════════════════════════════════

class FileSelectView(View):
    def __init__(self, contract_id: str, guild_id: int, files: list[dict]):
        super().__init__(timeout=120)
        self.cid = contract_id
        self.gid = guild_id
        self.files = files
        self.active_indices = set(range(len(files)))
        self.current_idx = 0

    def _generate_embed(self) -> discord.Embed:
        craft_exts = (".craft",)
        lines = []
        for i, f in enumerate(self.files):
            icon = "🚀" if f["filename"].lower().endswith(craft_exts) else "🖼️"
            status = "✅" if i in self.active_indices else "❌"
            pointer = "▶️" if i == self.current_idx else "  "
            lines.append(f"{pointer} {status} {icon} `{f['filename']}`")

        desc = "\n".join(lines)
        return discord.Embed(title="📎 Select files to submit", description=desc, color=discord.Color.blue())

    @button(emoji="⬆️", style=discord.ButtonStyle.grey, row=0)
    async def up_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.files:
            self.current_idx = (self.current_idx - 1) % len(self.files)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(emoji="⬇️", style=discord.ButtonStyle.grey, row=0)
    async def down_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.files:
            self.current_idx = (self.current_idx + 1) % len(self.files)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(emoji="🔄", label="Toggle Active", style=discord.ButtonStyle.blurple, row=0)
    async def toggle_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.current_idx in self.active_indices:
            self.active_indices.remove(self.current_idx)
        else:
            self.active_indices.add(self.current_idx)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(label="✅ Confirm & Send", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c.get("status") != cdb.ACTIVE:
            await interaction.followup.send("❌ Contract already submitted or no longer active.", ephemeral=True)
            return

        selected_files = [f for i, f in enumerate(self.files) if i in self.active_indices]
        if not selected_files:
            await interaction.followup.send("❌ You must select at least one file.", ephemeral=True)
            return

        has_image = any(f["content_type"].startswith("image/") for f in selected_files)

        if not has_image:
            await interaction.followup.send(
                "❌ Missing in selection: screenshot (image). Select at least a screenshot.",
                ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        # ── Flag-design contract → gate full-res, show watermarked preview ──
        if c.get("mission_type") == cdb.FLAG_DESIGN:
            await self._submit_flag(interaction, c, selected_files)
            return

        stored = []
        for f in selected_files:
            try:
                data = await cdb.download_url(f["url"])
                url = await cdb.upload_to_storage(self.cid, f["filename"], data, f.get("content_type", ""))
                stored.append({"filename": f["filename"], "url": url, "content_type": f.get("content_type", "")})
            except Exception as exc:
                log.error("Upload failed: %s", exc)
                stored.append({"filename": f["filename"], "url": f["url"], "content_type": f.get("content_type", "")})
        from datetime import datetime
        cdb.update_contract(self.gid, self.cid, status=cdb.SUBMITTED,
                            submitted_files=stored, submitted_at=datetime.utcnow().isoformat())
        c = cdb.get_contract(self.gid, self.cid)
        bot = interaction.client

        # ── Bot-issued contract (weekly missions) → AI auto-review ───────
        is_bot_issued = (
            str(c["issuer_id"]) == str(bot.user.id)
            or c.get("issuer_name", "").lower() == bot.user.display_name.lower()
        )
        log.info("Contract %s issuer_id=%s bot_id=%s is_bot=%s",
                 self.cid, c["issuer_id"], bot.user.id, is_bot_issued)
        if is_bot_issued:
            await self._ai_review(interaction, c, stored)
            return

        # ── Human-issued contract → DM issuer for review ─────────────────
        try:
            issuer = await bot.fetch_user(int(c["issuer_id"]))
            e = _embed(c, self.gid)
            e.title = f"📬 {t(self.gid, 'ct.review_title')}"
            e.color = discord.Color.orange()
            screenshots = [s for s in stored if not s['filename'].lower().endswith('.craft')]
            craft_count = len(stored) - len(screenshots)
            file_parts = []
            if craft_count:
                file_parts.append(f"🚀 {craft_count} craft file(s) *(revealed after acceptance)*")
            else:
                file_parts.append("⚠️ **WARNING: No craft file included!**")
            for s in screenshots:
                file_parts.append(f"🖼️ [{s['filename']}]({s['url']})")
            e.add_field(name="📁 Files", value="\n".join(file_parts) or "None", inline=False)
            view = ContractReviewView(self.cid, self.gid)
            msg = await issuer.send(embed=e, view=view)
            cdb.update_contract(self.gid, self.cid, issuer_review_msg_id=str(msg.id))
        except Exception as exc:
            log.error("Could not DM issuer: %s", exc)
        # Update contractor panel
        if c.get("dm_message_id"):
            try:
                orig = await interaction.channel.fetch_message(int(c["dm_message_id"]))
                c["status"] = cdb.SUBMITTED
                await orig.edit(embed=_embed(c, self.gid), view=None)
            except Exception:
                pass
        await interaction.followup.send("✅ Submitted!", ephemeral=True)

    async def _submit_flag(self, interaction: discord.Interaction, c: dict, selected_files: list[dict]):
        """Flag-design submission: keep the clean image gated, surface only a
        watermarked preview, and DM the issuer for review. Flag contracts are
        always human-issued, so there's no AI auto-review path."""
        from datetime import datetime
        import flag_preview

        img = next((f for f in selected_files if f["content_type"].startswith("image/")), None)
        if not img:
            await interaction.followup.send("❌ No image found to submit as the flag.", ephemeral=True)
            return

        try:
            raw = await cdb.download_url(img["url"])
        except Exception as exc:
            log.error("Flag download failed: %s", exc)
            await interaction.followup.send("❌ Could not read your uploaded flag. Try again.", ephemeral=True)
            return

        # Full-res stays gated; only the watermarked preview is shown until accept.
        fullres_url = await cdb.upload_to_storage(self.cid, img["filename"], raw,
                                                  img.get("content_type", "image/png"))
        preview_url = await cdb.upload_to_storage(
            self.cid, "flag_preview.png", flag_preview.make_watermarked(raw), "image/png")

        cdb.update_contract(self.gid, self.cid, status=cdb.SUBMITTED,
                            submitted_files=[], flag_filename=img["filename"],
                            flag_fullres_url=fullres_url, flag_preview_url=preview_url,
                            submitted_at=datetime.utcnow().isoformat())
        c = cdb.get_contract(self.gid, self.cid)

        try:
            issuer = await interaction.client.fetch_user(int(c["issuer_id"]))
            e = _embed(c, self.gid)
            e.title = f"📬 {t(self.gid, 'ct.review_title')}"
            e.color = discord.Color.orange()
            e.add_field(
                name="🚩 Flag",
                value="Preview is watermarked; the full-res flag is delivered to your "
                      "in-game flag picker on acceptance.",
                inline=False)
            msg = await issuer.send(embed=e, view=ContractReviewView(self.cid, self.gid))
            cdb.update_contract(self.gid, self.cid, issuer_review_msg_id=str(msg.id))
        except Exception as exc:
            log.error("Could not DM issuer for flag review: %s", exc)

        # Update the designer's contract panel.
        if c.get("dm_message_id"):
            try:
                orig = await interaction.channel.fetch_message(int(c["dm_message_id"]))
                await orig.edit(embed=_embed(c, self.gid), view=None)
            except Exception:
                pass
        await interaction.followup.send("✅ Flag submitted for review!", ephemeral=True)

    async def _ai_review(self, interaction: discord.Interaction, c: dict, stored: list[dict]):
        """Use Gemini AI to review screenshots against the mission description."""
        import aiohttp
        screenshots = [s for s in stored if s.get("content_type", "").startswith("image/")]
        if not screenshots:
            await interaction.followup.send("❌ No screenshots found for AI review.", ephemeral=True)
            return

        # Download screenshot bytes
        img_bytes_list = []
        for s in screenshots:
            try:
                raw = await cdb.download_url(s["url"])
                img_bytes_list.append(raw)
            except Exception:
                pass

        if not img_bytes_list:
            await interaction.followup.send("❌ Could not download screenshots.", ephemeral=True)
            return

        # Build AI review prompt
        mission_desc = c.get("mission", "")
        from cogs.screenshots import active_client, record_gemini, _MODEL
        from google.genai import types
        import json

        gemini_client = active_client()
        if not gemini_client:
            # Fallback: auto-accept if no Gemini (key missing OR budget reached)
            await self._auto_accept(interaction, c)
            return

        review_prompt = (
            f"You are reviewing a KSP contract submission.\n"
            f"The mission was: \"{mission_desc}\"\n\n"
            f"Analyze the screenshot(s) and determine if the mission was completed successfully.\n"
            f"CRITICAL RULES FOR SPACE ELEVATORS:\n"
            f"- In KSP, space elevators are built as extremely tall towers or tethers attached to the ground and stretching endlessly into the sky.\n"
            f"- If the mission involves a space elevator/tether, and you see a tall vertical structure reaching into the sky, you MUST ACCEPT IT.\n"
            f"- DO NOT reject it by claiming it looks like a 'static ground tower' or 'lacks evidence of altitude/functionality'. A ground-anchored tower stretching up IS the visual proof of a space elevator in KSP.\n"
            f"- Be highly lenient. If it remotely looks like the requested structure, approve it.\n\n"
            f"Additionally, assign the highest applicable KSP achievement level (1-15) based on the mission and screenshot.\n"
            f"1. Kerbin Orbit | 2. Mun Landing | 3. Docking (Space Stations) | 4. Duna Landing | 5. RSS Earth Orbit\n"
            f"6. Eve Landing | 7. Asteroid Redirect | 8. RSS Moon Landing | 9. Jool 5 | 10. Interstellar Mission\n"
            f"11. RSS Mars | 12. RSS Venus Landing | 13. RSS Gas Giant | 14. Kerbol Grand Tour | 15. RSS Interstellar\n"
            f"If none clearly apply, set ksp_level to 0.\n\n"
            f"Return ONLY valid JSON:\n"
            f'{{\n  "approved": true/false,\n  "reason": "brief explanation in the same language as the mission description",\n  "ksp_level": integer\n}}'
        )

        parts = [types.Part.from_text(text=review_prompt)]
        for img in img_bytes_list:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))

        try:
            response = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=512),
            )
            record_gemini(response)
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            result = json.loads(raw.strip())
        except Exception as exc:
            log.error("AI review failed: %s", exc)
            # Fallback: auto-accept on AI failure
            await self._auto_accept(interaction, c)
            return

        if result.get("approved", False):
            await self._auto_accept(interaction, c, result.get("reason", ""), result.get("ksp_level", 0))
        else:
            await self._auto_refuse(interaction, c, result.get("reason", ""))

    async def _auto_accept(self, interaction: discord.Interaction, c: dict, reason: str = "", ksp_level: int = 0):
        from datetime import datetime
        cdb.update_contract(self.gid, self.cid, status=cdb.COMPLETED,
                            completed_at=datetime.utcnow().isoformat())
        await store.add_balance(self.gid, int(c["contractor_id"]), c["payment"])
        # Grant XP too for weekly missions
        diff = c["payment"] // settings.WEEKLY_COINS_PER_DIFFICULTY if settings.WEEKLY_COINS_PER_DIFFICULTY else 0
        xp = diff * settings.WEEKLY_XP_PER_DIFFICULTY
        if xp > 0:
            user = store.get_user(self.gid, int(c["contractor_id"]))
            from data.store import store as _store
            await _store.set_xp(self.gid, int(c["contractor_id"]), user["xp"] + xp)

        if ksp_level > 0:
            from cogs.roles import check_and_award_level
            interaction.client.loop.create_task(
                check_and_award_level(interaction.client, self.gid, int(c["contractor_id"]), ksp_level)
            )

        sym = settings.CURRENCY_SYMBOL
        e = discord.Embed(
            title=f"✅ {t(self.gid, 'ct.accepted')}",
            description=f"{reason}\n\n**+{c['payment']}** {sym} · **+{xp} XP**" if reason else f"**+{c['payment']}** {sym} · **+{xp} XP**",
            color=discord.Color.green(),
        )
        # Update the contract message in corp channel
        if c.get("dm_message_id"):
            try:
                ch = interaction.channel or await interaction.client.fetch_channel(interaction.channel_id)
                orig = await ch.fetch_message(int(c["dm_message_id"]))
                c["status"] = cdb.COMPLETED
                await orig.edit(embed=_embed(c, self.gid), view=None)
            except Exception:
                pass
        await interaction.followup.send(embed=e, ephemeral=True)
        log.info("AI auto-accepted contract %s", self.cid)

    async def _auto_refuse(self, interaction: discord.Interaction, c: dict, reason: str = ""):
        cdb.update_contract(self.gid, self.cid, status=cdb.DISPUTED)
        e = discord.Embed(
            title=f"❌ {t(self.gid, 'ct.disputed')}",
            description=reason or t(self.gid, "ct.disputed_desc"),
            color=discord.Color.red(),
        )
        e.set_footer(text=t(self.gid, "ct.disputed_desc"))
        # Update corp channel message
        if c.get("dm_message_id"):
            try:
                ch = interaction.channel or await interaction.client.fetch_channel(interaction.channel_id)
                orig = await ch.fetch_message(int(c["dm_message_id"]))
                c["status"] = cdb.DISPUTED
                await orig.edit(embed=_embed(c, self.gid), view=DisputeView(self.cid, self.gid))
            except Exception:
                pass
        await interaction.followup.send(embed=e, ephemeral=True)
        log.info("AI auto-refused contract %s: %s", self.cid, reason)


# ── More Time Modal ──────────────────────────────────────────────────────────

class MoreTimeModal(discord.ui.Modal, title="Extend Deadline"):
    new_date = discord.ui.TextInput(label="New due date (YYYY-MM-DD)", placeholder="2025-06-30")

    def __init__(self, contract_id: str, guild_id: int):
        super().__init__()
        self.cid = contract_id
        self.gid = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from datetime import datetime, date
        try:
            new_dt = datetime.strptime(self.new_date.value, "%Y-%m-%d").date()
        except ValueError:
            await interaction.followup.send("❌ Invalid format. Use YYYY-MM-DD.", ephemeral=True)
            return
        if new_dt <= date.today():
            await interaction.followup.send("❌ Date must be in the future.", ephemeral=True)
            return
        c = cdb.get_contract(self.gid, self.cid)
        if not c:
            return
        try:
            issuer = await interaction.client.fetch_user(int(c["issuer_id"]))
            e = discord.Embed(
                title=f"⏰ {t(self.gid, 'ct.moretime_request')}",
                description=t(self.gid, 'ct.moretime_desc',
                              name=c['contractor_name'],
                              old=c['due_date'], new=self.new_date.value),
                color=discord.Color.blue(),
            )
            await issuer.send(embed=e, view=MoreTimeApprovalView(self.cid, self.gid, self.new_date.value))
            await interaction.followup.send(f"⏰ Extension request sent ({self.new_date.value}).", ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ Could not contact issuer.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  All DynamicItem classes for registration
# ══════════════════════════════════════════════════════════════════════════════

ALL_DYNAMIC_ITEMS = [
    AcceptOfferButton, RefuseOfferButton,
    GiveUpButton, SubmitButton,
    ReviewAcceptButton, ReviewRefuseButton,
    SettleButton, MoreTimeButton, PayFineButton, SueButton,
    MoreTimeApproveButton, MoreTimeRefuseButton,
    SettleApproveButton, SettleRefuseButton,
    ModEnforceButton, ModCancelButton,
]
