"""Contract interactive button views.

All views use persistent custom_ids so buttons survive bot restarts.
Contract/guild info is encoded in the custom_id: "prefix:contract_id:guild_id"
"""
import logging
import discord
from discord.ui import View, Button, button
from i18n import t, tp
import settings
from data.store import store
from data import contracts as cdb

log = logging.getLogger(__name__)


def _cid(custom_id_base: str, contract_id: str, guild_id: int) -> str:
    return f"{custom_id_base}:{contract_id}:{guild_id}"


def _parse(custom_id: str) -> tuple[str, int]:
    """Extract (contract_id, guild_id) from a custom_id like 'prefix:cid:gid'."""
    parts = custom_id.split(":")
    return parts[1], int(parts[2])


def _embed(c, guild_id):
    e = discord.Embed(title=f"📜 {t(guild_id, 'ct.title')}", color=discord.Color.gold())
    sym = settings.CURRENCY_SYMBOL
    e.add_field(name=t(guild_id, "ct.mission"), value=c["mission"], inline=False)
    e.add_field(name=t(guild_id, "ct.issuer"), value=c["issuer_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.contractor"), value=c["contractor_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.payment"), value=f"**{c['payment']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.fine"), value=f"**{c['fine']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.due"), value=c["due_date"], inline=True)
    e.add_field(name=t(guild_id, "ct.status"), value=f"`{c['status']}`", inline=True)
    return e


# ── Offer View (Accept / Refuse) ────────────────────────────────────────────

class ContractOfferView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        # Dynamic custom_ids for persistence
        self.accept_btn.custom_id = _cid("ct_accept", contract_id, guild_id)
        self.refuse_btn.custom_id = _cid("ct_refuse", contract_id, guild_id)

    @button(label="✅ Accept", style=discord.ButtonStyle.green, custom_id="ct_accept::")
    async def accept_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c or c["status"] != cdb.PENDING:
            await interaction.response.send_message("❌", ephemeral=True)
            return
        cdb.update_contract(gid, cid, status=cdb.ACTIVE)
        c["status"] = cdb.ACTIVE
        e = _embed(c, gid)
        e.color = discord.Color.green()
        await interaction.response.edit_message(embed=e, view=ContractWorkView(cid, gid))

    @button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="ct_refuse::")
    async def refuse_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        cdb.update_contract(gid, cid, status=cdb.CANCELLED)
        await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
        c["status"] = cdb.CANCELLED
        e = _embed(c, gid)
        e.color = discord.Color.red()
        await interaction.response.edit_message(embed=e, view=None)


# ── Work View (Give Up / Submit) ─────────────────────────────────────────────

class ContractWorkView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.giveup_btn.custom_id = _cid("ct_giveup", contract_id, guild_id)
        self.submit_btn.custom_id = _cid("ct_submit", contract_id, guild_id)

    @button(label="🏳️ Give Up", style=discord.ButtonStyle.grey, custom_id="ct_giveup::")
    async def giveup_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c or c["status"] != cdb.ACTIVE:
            return
        cdb.update_contract(gid, cid, status=cdb.CANCELLED)
        await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
        c["status"] = cdb.CANCELLED
        e = _embed(c, gid)
        e.color = discord.Color.red()
        await interaction.response.edit_message(embed=e, view=None)

    @button(label="📤 Submit", style=discord.ButtonStyle.blurple, custom_id="ct_submit::")
    async def submit_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c or c["status"] != cdb.ACTIVE:
            return
        # Scan DM for files after the contract message
        files_found = []
        dm_msg_id = int(c.get("dm_message_id") or 0)
        async for msg in interaction.channel.history(limit=50, after=discord.Object(id=dm_msg_id)):
            if msg.author.id == interaction.user.id:
                for att in msg.attachments:
                    files_found.append({"url": att.url, "filename": att.filename,
                                        "content_type": att.content_type or "application/octet-stream"})
        if not files_found:
            await interaction.response.send_message("❌ No files found. Upload files here first.", ephemeral=True)
            return
        # Require at least one image (screenshot)
        has_image = any(f["content_type"].startswith("image/") for f in files_found)
        if not has_image:
            await interaction.response.send_message(
                "❌ Missing screenshot (image). Upload at least a screenshot.",
                ephemeral=True)
            return
        view = FileSelectView(cid, gid, files_found)
        await interaction.response.send_message(embed=view._generate_embed(), view=view, ephemeral=True)


# ── File Selection (ephemeral, no persistence needed) ────────────────────────

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
        if self.files:
            self.current_idx = (self.current_idx - 1) % len(self.files)
        await interaction.response.edit_message(embed=self._generate_embed(), view=self)

    @button(emoji="⬇️", style=discord.ButtonStyle.grey, row=0)
    async def down_btn(self, interaction: discord.Interaction, btn: Button):
        if self.files:
            self.current_idx = (self.current_idx + 1) % len(self.files)
        await interaction.response.edit_message(embed=self._generate_embed(), view=self)

    @button(emoji="🔄", label="Toggle Active", style=discord.ButtonStyle.blurple, row=0)
    async def toggle_btn(self, interaction: discord.Interaction, btn: Button):
        if self.current_idx in self.active_indices:
            self.active_indices.remove(self.current_idx)
        else:
            self.active_indices.add(self.current_idx)
        await interaction.response.edit_message(embed=self._generate_embed(), view=self)

    @button(label="✅ Confirm & Send", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, btn: Button):
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c.get("status") != cdb.ACTIVE:
            await interaction.response.send_message("❌ Contract already submitted or no longer active.", ephemeral=True)
            return

        selected_files = [f for i, f in enumerate(self.files) if i in self.active_indices]
        if not selected_files:
            await interaction.response.send_message("❌ You must select at least one file.", ephemeral=True)
            return

        has_image = any(f["content_type"].startswith("image/") for f in selected_files)

        if not has_image:
            await interaction.response.send_message(
                "❌ Missing in selection: screenshot (image). Select at least a screenshot.",
                ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

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
            e.add_field(name="📁 Files", value="\n".join(file_parts) or "—", inline=False)
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
        from cogs.screenshots import _client as gemini_client, _MODEL
        from google.genai import types
        import json

        if not gemini_client:
            # Fallback: auto-accept if no Gemini
            await self._auto_accept(interaction, c)
            return

        review_prompt = (
            f"You are reviewing a KSP contract submission.\n"
            f"The mission was: \"{mission_desc}\"\n\n"
            f"Analyze the screenshot(s) and determine if the mission was completed successfully.\n"
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


# ── Review View (Issuer accepts/refuses) ─────────────────────────────────────

class ContractReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.accept_btn.custom_id = _cid("ct_rv_acc", contract_id, guild_id)
        self.refuse_btn.custom_id = _cid("ct_rv_ref", contract_id, guild_id)

    @button(label="✅ Accept", style=discord.ButtonStyle.green, custom_id="ct_rv_acc::")
    async def accept_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c or c["status"] != cdb.SUBMITTED:
            return
        # Only the issuer can review
        if str(interaction.user.id) != str(c.get("issuer_id")):
            await interaction.response.send_message("❌ Only the contract issuer can review submissions.", ephemeral=True)
            return
        from datetime import datetime
        cdb.update_contract(gid, cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        await store.add_balance(gid, int(c["contractor_id"]), c["payment"])
        c["status"] = cdb.COMPLETED
        e = _embed(c, gid)
        e.color = discord.Color.green()
        # NOW reveal the craft files (screenshots were already visible)
        files = c.get("submitted_files", [])
        craft_files = [s for s in files if s['filename'].lower().endswith('.craft')]
        if craft_files:
            flist = "\n".join(f"🚀 [{s['filename']}]({s['url']})" for s in craft_files)
            e.add_field(name="📁 Craft Files", value=flist, inline=False)
        screenshots = [s for s in files if not s['filename'].lower().endswith('.craft')]
        if screenshots:
            flist = "\n".join(f"🖼️ [{s['filename']}]({s['url']})" for s in screenshots)
            e.add_field(name="🖼️ Screenshots", value=flist, inline=False)
        await interaction.response.edit_message(embed=e, view=None)
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            ne = discord.Embed(title=f"✅ {t(gid, 'ct.accepted')}",
                               description=t(gid, 'ct.accepted_desc', payment=c['payment'], sym=settings.CURRENCY_SYMBOL),
                               color=discord.Color.green())
            await contractor.send(embed=ne)
        except Exception:
            pass

    @button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="ct_rv_ref::")
    async def refuse_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c or c["status"] != cdb.SUBMITTED:
            return
        # Only the issuer can review
        if str(interaction.user.id) != str(c.get("issuer_id")):
            await interaction.response.send_message("❌ Only the contract issuer can review submissions.", ephemeral=True)
            return
        cdb.update_contract(gid, cid, status=cdb.DISPUTED)
        c["status"] = cdb.DISPUTED
        e = _embed(c, gid)
        e.color = discord.Color.red()
        await interaction.response.edit_message(embed=e, view=None)
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            de = discord.Embed(title=f"⚠️ {t(gid, 'ct.disputed')}",
                               description=t(gid, 'ct.disputed_desc'), color=discord.Color.orange())
            await contractor.send(embed=de, view=DisputeView(cid, gid))
        except Exception:
            pass


# ── Dispute View (Settle / More Time / Pay Fine / Sue) ───────────────────────

class DisputeView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.settle_btn.custom_id = _cid("ct_settle", contract_id, guild_id)
        self.moretime_btn.custom_id = _cid("ct_moretime", contract_id, guild_id)
        self.payfine_btn.custom_id = _cid("ct_payfine", contract_id, guild_id)
        self.sue_btn.custom_id = _cid("ct_sue", contract_id, guild_id)

    @button(label="🤝 Settle", style=discord.ButtonStyle.grey, custom_id="ct_settle::")
    async def settle_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        # Bot-issued contracts can't be settled
        if str(c["issuer_id"]) == str(interaction.client.user.id):
            await interaction.response.send_message("❌ AI contracts cannot be settled.", ephemeral=True)
            return
        try:
            issuer = await interaction.client.fetch_user(int(c["issuer_id"]))
            e = discord.Embed(title=f"🤝 {t(gid, 'ct.settle_request')}",
                              description=t(gid, 'ct.settle_desc', name=c['contractor_name']),
                              color=discord.Color.light_grey())
            await issuer.send(embed=e, view=SettleApprovalView(cid, gid))
            await interaction.response.send_message(t(gid, "ct.settle_sent"), ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌", ephemeral=True)

    @button(label="⏰ More Time", style=discord.ButtonStyle.grey, custom_id="ct_moretime::")
    async def moretime_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        # Bot-issued: auto-extend to end of week, no modal
        if c and str(c["issuer_id"]) == str(interaction.client.user.id):
            from datetime import datetime, timedelta, timezone
            tz = timezone(timedelta(hours=3))
            now = datetime.now(tz)
            # End of week = next Sunday (day before Monday reset)
            days_to_sunday = 6 - now.weekday()  # Sunday = 6
            if days_to_sunday <= 0:
                days_to_sunday = 7
            end_of_week = (now + timedelta(days=days_to_sunday)).strftime("%Y-%m-%d")
            cdb.update_contract(gid, cid, due_date=end_of_week, status=cdb.ACTIVE)
            # Re-show work view on the contract message
            if c.get("dm_message_id"):
                try:
                    ch = interaction.channel or await interaction.client.fetch_channel(interaction.channel_id)
                    orig = await ch.fetch_message(int(c["dm_message_id"]))
                    c["status"] = cdb.ACTIVE
                    c["due_date"] = end_of_week
                    await orig.edit(embed=_embed(c, gid), view=ContractWorkView(cid, gid))
                except Exception:
                    pass
            await interaction.response.edit_message(
                content=f"⏰ Extended to **{end_of_week}**. Submit again!",
                embed=None, view=None,
            )
            return
        # Normal contract: open date modal
        await interaction.response.send_modal(MoreTimeModal(cid, gid))

    @button(label="💰 Pay Fine", style=discord.ButtonStyle.red, custom_id="ct_payfine::")
    async def payfine_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        bal = store.get_user(gid, int(c["contractor_id"]))["balance"]
        if bal < c["fine"]:
            await interaction.response.send_message(t(gid, "ct.no_funds"), ephemeral=True)
            return
        await store.add_balance(gid, int(c["contractor_id"]), -c["fine"])
        await store.add_balance(gid, int(c["issuer_id"]), c["fine"] + c["payment"])
        from datetime import datetime
        cdb.update_contract(gid, cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        c["status"] = cdb.COMPLETED
        e = _embed(c, gid)
        e.color = discord.Color.dark_red()
        e.set_footer(text=t(gid, "ct.fine_paid"))
        await interaction.response.edit_message(embed=e, view=None)

    @button(label="⚖️ Sue", style=discord.ButtonStyle.blurple, custom_id="ct_sue::")
    async def sue_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        mod_ch_id = settings.CONTRACT_MOD_CHANNEL_ID
        if not mod_ch_id:
            await interaction.response.send_message("❌ Not configured.", ephemeral=True)
            return
        cdb.update_contract(gid, cid, status=cdb.MOD_REVIEW)
        bot = interaction.client
        ch = bot.get_channel(mod_ch_id) or await bot.fetch_channel(mod_ch_id)
        c["status"] = cdb.MOD_REVIEW
        e = _embed(c, gid)
        e.title = f"⚖️ {t(gid, 'ct.mod_review')}"
        e.color = discord.Color.purple()
        files = c.get("submitted_files", [])
        if files:
            e.add_field(name="📁", value="\n".join(f"📎 [{f['filename']}]({f['url']})" for f in files), inline=False)
        await ch.send(embed=e, view=ModReviewView(cid, gid))
        await interaction.response.edit_message(content=t(gid, "ct.sued"), view=None)


# ── More Time Modal ──────────────────────────────────────────────────────────

class MoreTimeModal(discord.ui.Modal, title="Extend Deadline"):
    new_date = discord.ui.TextInput(label="New due date (YYYY-MM-DD)", placeholder="2025-06-30")

    def __init__(self, contract_id: str, guild_id: int):
        super().__init__()
        self.cid = contract_id
        self.gid = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        from datetime import datetime, date
        # Validate format
        try:
            new_dt = datetime.strptime(self.new_date.value, "%Y-%m-%d").date()
        except ValueError:
            await interaction.response.send_message("❌ Invalid format. Use YYYY-MM-DD.", ephemeral=True)
            return
        # Must be in the future
        if new_dt <= date.today():
            await interaction.response.send_message("❌ Date must be in the future.", ephemeral=True)
            return
        # Don't apply yet — send approval to issuer
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
            await interaction.response.send_message(f"⏰ Extension request sent ({self.new_date.value}).", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Could not contact issuer.", ephemeral=True)


# ── More Time Approval View ──────────────────────────────────────────────────

class MoreTimeApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0, new_date: str = ""):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.new_date = new_date
        self.yes_btn.custom_id = _cid("ct_mt_y", contract_id, guild_id)
        self.no_btn.custom_id = _cid("ct_mt_n", contract_id, guild_id)

    @button(label="✅ Approve Extension", style=discord.ButtonStyle.green, custom_id="ct_mt_y::")
    async def yes_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        # Extract new_date from embed description if reloaded after restart
        new_date = self.new_date
        if not new_date:
            # Fallback: parse from the embed
            if interaction.message and interaction.message.embeds:
                desc = interaction.message.embeds[0].description or ""
                # last word is the date
                new_date = desc.strip().split()[-1]
        cdb.update_contract(gid, cid, due_date=new_date, status=cdb.ACTIVE)
        await interaction.response.edit_message(
            content=f"✅ Deadline extended to **{new_date}**. Contract is active again.",
            embed=None, view=None,
        )
        # Notify contractor and give them the work view back
        try:
            contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
            c["status"] = cdb.ACTIVE
            c["due_date"] = new_date
            e = _embed(c, gid)
            e.color = discord.Color.green()
            await contractor.send(embed=e, view=ContractWorkView(cid, gid))
        except Exception:
            pass

    @button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="ct_mt_n::")
    async def no_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        await interaction.response.edit_message(
            content=f"❌ Extension refused.", embed=None, view=None,
        )
        # Notify contractor — dispute view stays
        c = cdb.get_contract(gid, cid)
        if c:
            try:
                contractor = await interaction.client.fetch_user(int(c["contractor_id"]))
                await contractor.send("❌ Your time extension request was refused.")
            except Exception:
                pass


# ── Settle Approval View ─────────────────────────────────────────────────────

class SettleApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.yes_btn.custom_id = _cid("ct_stl_y", contract_id, guild_id)
        self.no_btn.custom_id = _cid("ct_stl_n", contract_id, guild_id)

    @button(label="✅ Accept Settlement", style=discord.ButtonStyle.green, custom_id="ct_stl_y::")
    async def yes_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
        cdb.update_contract(gid, cid, status=cdb.CANCELLED)
        await interaction.response.edit_message(content=f"✅ {t(gid, 'ct.settled')}", embed=None, view=None)

    @button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="ct_stl_n::")
    async def no_btn(self, interaction: discord.Interaction, btn: Button):
        _, gid = _parse(btn.custom_id)
        await interaction.response.edit_message(content=f"❌ {t(gid, 'ct.settle_refused')}", embed=None, view=None)


# ── Mod Review View ──────────────────────────────────────────────────────────

class ModReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.cid = contract_id
        self.gid = guild_id
        self.enforce_btn.custom_id = _cid("ct_mod_f", contract_id, guild_id)
        self.cancel_btn.custom_id = _cid("ct_mod_c", contract_id, guild_id)

    @button(label="✅ Enforce Fine", style=discord.ButtonStyle.green, custom_id="ct_mod_f::")
    async def enforce_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        bal = store.get_user(gid, int(c["contractor_id"]))["balance"]
        fine = min(c["fine"], bal)
        if fine > 0:
            await store.add_balance(gid, int(c["contractor_id"]), -fine)
            await store.add_balance(gid, int(c["issuer_id"]), fine)
        await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
        from datetime import datetime
        cdb.update_contract(gid, cid, status=cdb.COMPLETED, completed_at=datetime.utcnow().isoformat())
        await interaction.response.edit_message(content=f"✅ Fine enforced ({fine}). Escrow refunded.", view=None)

    @button(label="❌ Cancel Fine", style=discord.ButtonStyle.red, custom_id="ct_mod_c::")
    async def cancel_btn(self, interaction: discord.Interaction, btn: Button):
        cid, gid = _parse(btn.custom_id)
        c = cdb.get_contract(gid, cid)
        if not c:
            return
        await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
        cdb.update_contract(gid, cid, status=cdb.CANCELLED)
        await interaction.response.edit_message(content="❌ Fine cancelled. Escrow refunded.", view=None)
