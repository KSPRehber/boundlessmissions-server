"""
cogs/weeklymissions.py – Weekly mission board.

Posts a persistent embed with 20 randomly-generated missions.
Players select via buttons → contract created in their corp channel.
AI reviews submissions. Resets every Monday 00:00 GMT+3.
"""

import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import settings
from data.store import _db, store
from data import guild_config
from data.mission_templates import TEMPLATES
from i18n import t, S
from cogs.corps import _get_corp

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=3))  # GMT+3

# The contract posted here carries no Submit button any more: a mission is finished in
# KSP and submitted from the mod, which is the only front end that can send the craft
# and the telemetry the AI review reads. Said on the card so a corp channel is never a
# dead end.
SUBMIT_IN_KSP = (
    "Fly it, then open the mod's sidebar → **Contracts** → **Submit**. "
    "Submissions no longer happen in Discord."
)

S.update({
    "wm.title":        {"en": "📋 Weekly Missions"},
    "wm.week":         {"en": "Week {n} ({start} to {end})"},
    "wm.locked":       {"en": "🔒 Mission selection is locked."},
    "wm.no_corp":      {"en": "❌ You need a corporation first! Use `/g corpsetup`."},
    "wm.already":      {"en": "❌ You already selected this mission."},
    "wm.accepted":     {"en": "✅ Mission #{n} accepted! Contract posted to {channel}."},
    "wm.easy":         {"en": "🟢 Easy"},
    "wm.medium":       {"en": "🟡 Medium"},
    "wm.hard":         {"en": "🔴 Hard"},
    "wm.extreme":      {"en": "⚫ Extreme"},
    "wm.closes":       {"en": "⏰ Selection closes"},
    "wm.contract_title": {"en": "📋 Weekly Mission #{n}"},
    "wm.account_unavailable": {"en": "⚠️ Couldn't look your account up just now. Try again in a moment."},
    "wm.starting_up":  {"en": "⏳ The bot is still starting up. Try again in a moment."},
    "wm.custom_accepted": {"en": "✅ Custom mission accepted! Contract posted to {channel}."},
})


# ── Week helpers ─────────────────────────────────────────────────────────────

def _week_key(now: datetime | None = None) -> str:
    """Return 'YYYY-WNN' for the current week (Mon-based, GMT+3)."""
    if now is None:
        now = datetime.now(TZ)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (monday_00:00, next_monday_00:00) in GMT+3."""
    if now is None:
        now = datetime.now(TZ)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, monday + timedelta(days=7)


def _is_locked(now: datetime | None = None) -> bool:
    """True if we're on the last day (Sunday) of the week in GMT+3."""
    if now is None:
        now = datetime.now(TZ)
    return now.weekday() == 6  # Sunday


# ── Mission generation ───────────────────────────────────────────────────────

def _generate_missions(week_key: str, count: int = 20) -> list[dict]:
    """Deterministic random selection of missions for a given week."""
    seed = int(hashlib.md5(week_key.encode()).hexdigest(), 16)
    rng = random.Random(seed)

    easy = [t for t in TEMPLATES if t[2] <= 3]
    medium = [t for t in TEMPLATES if 4 <= t[2] <= 6]
    hard = [t for t in TEMPLATES if 7 <= t[2] <= 8]
    extreme = [t for t in TEMPLATES if t[2] >= 9]

    # Distribution: ~6 easy, ~6 medium, ~5 hard, ~3 extreme
    pick = []
    pick += rng.sample(easy, min(6, len(easy)))
    pick += rng.sample(medium, min(6, len(medium)))
    pick += rng.sample(hard, min(5, len(hard)))
    pick += rng.sample(extreme, min(3, len(extreme)))

    # Sort by difficulty
    pick.sort(key=lambda x: x[2])

    missions = []
    for i, (desc_en, desc_tr, diff, cat) in enumerate(pick[:count], 1):
        xp = diff * settings.WEEKLY_XP_PER_DIFFICULTY
        coins = diff * settings.WEEKLY_COINS_PER_DIFFICULTY
        fine = int(coins * settings.WEEKLY_FINE_PERCENT / 100)
        missions.append({
            "id": i,
            "desc_en": desc_en,
            "desc_tr": desc_tr,
            "difficulty": diff,
            "category": cat,
            "xp": xp,
            "coins": coins,
            "fine": fine,
        })
    return missions


# ── Firestore helpers ────────────────────────────────────────────────────────

def _missions_ref(guild_id: int, week_key: str):
    return _db.collection("guilds").document(str(guild_id)).collection("weekly_missions").document(week_key)


def _save_missions(guild_id: int, week_key: str, missions: list[dict], msg_id: int):
    _missions_ref(guild_id, week_key).set({
        "missions": missions,
        "embed_message_id": str(msg_id),
        "generated_at": datetime.now(TZ).isoformat(),
    })


def _load_missions(guild_id: int, week_key: str) -> tuple[list[dict], int | None]:
    snap = _missions_ref(guild_id, week_key).get()
    if not snap.exists:
        return [], None
    d = snap.to_dict()
    return d.get("missions", []), int(d["embed_message_id"]) if d.get("embed_message_id") else None


def _selection_ref(guild_id: int, week_key: str, user_id: int, mission_id: int):
    """The claim on one (week, player, mission).

    Top-level and **guild-independent**, the same move the wallet made to
    `users/{user_id}`, and for the same reason: what this claim protects is the
    payout, and the payout is global. It used to live under
    `guilds/{guild_id}/weekly_selections`, while `_generate_missions` seeds mission
    ids 1..20 from the week alone — so the same mission existed in every guild and
    a player in two of them held two different claim documents for it. Both
    succeeded, both minted a bot-issued contract, and the weekly coins and XP paid
    twice (N guilds, N times) into one wallet. `guild_id` is kept as a field,
    because where the mission was selected is still worth knowing.
    """
    doc_id = f"{week_key}_{user_id}_{mission_id}"
    return _db.collection("weekly_selections").document(doc_id)


def migrate_guild_selections() -> int:
    """Copy this week's per-guild selection claims to the top-level collection.

    `_selection_ref` moved from `guilds/{gid}/weekly_selections` to a top-level
    collection so the claim is guild-independent — the wallet it protects always was
    (see the docstring there). Without this, every claim already made in the current
    week is orphaned on deploy: `_has_selected` reads the new path, finds nothing,
    and every player can select each mission a second time, minting a second
    bot-issued contract that pays the weekly coins and XP again. That is the exact
    double-pay the move exists to prevent, delivered once to the whole community
    instead of only to players in two guilds.

    Only the CURRENT week is copied: older claims can no longer be re-selected (the
    week key is part of the document id), so moving them would be work for nothing.
    Idempotent — the ids are identical by construction and `set()` is last-writer-wins
    with the same content — so a second run, or a run after a partial one, is safe.
    Returns the number of claims carried over.
    """
    wk = _week_key()
    moved = 0
    try:
        for gdoc in _db.collection("guilds").stream():
            col = gdoc.reference.collection("weekly_selections")
            for doc in col.stream():
                if not doc.id.startswith(wk + "_"):
                    continue
                data = doc.to_dict() or {}
                data.setdefault("guild_id", gdoc.id)
                _db.collection("weekly_selections").document(doc.id).set(data)
                moved += 1
    except Exception as exc:
        # Best effort: a failure here means some players can re-select once this
        # week, which is bad but not worth refusing to start the bot over.
        log.error("Weekly-selection migration failed: %s", exc)
    if moved:
        log.warning("Weekly selections: carried %d claim(s) for week %s to the "
                    "top-level collection.", moved, wk)
    return moved


def _has_selected(guild_id: int, week_key: str, user_id: int, mission_id: int) -> bool:
    return _selection_ref(guild_id, week_key, user_id, mission_id).get().exists


def _save_selection(guild_id: int, week_key: str, user_id: int, mission_id: int) -> bool:
    """Record the selection — and CLAIM it.

    The document is created with `create()`, which fails when it already exists,
    so two requests for the same (week, player, mission) that both passed
    `_has_selected` cannot both get through: the second returns False and must
    not create a contract. Both selection paths (the KSP endpoint and the Discord
    button) call this *before* the contract is created, so the claim is the
    guard rather than a note written after the fact. Returns True when this call
    made the claim.
    """
    from google.api_core.exceptions import AlreadyExists
    try:
        _selection_ref(guild_id, week_key, user_id, mission_id).create({
            "user_id": str(user_id),
            "guild_id": str(guild_id),
            "mission_id": mission_id,
            "selected_at": datetime.now(TZ).isoformat(),
            "status": "active",
        })
    except AlreadyExists:
        return False
    return True


def link_selection_contract(guild_id: int, week_key: str, user_id: int,
                            mission_id: int, contract_id: str) -> None:
    """Record which contract a claim minted.

    The claim is the ONLY thing preventing a weekly mission being selected — and so
    paid — twice; `_has_selected` is the sole gate, and a bot-issued contract has no
    escrow behind it, so a second payout is a straight mint. That made
    `/contractreset` dangerous: it deleted every claim the account held, including
    the one for a mission already completed and PAID this week, and the loop directly
    above it in the same command is careful to skip terminal contracts for exactly
    this reason.

    Nothing on the claim said which contract it belonged to, so the reset had no way
    to be careful. This writes the link at the moment it is known. Claims created
    before this field existed carry no `contract_id` and are deliberately treated as
    un-resettable rather than as free to delete — see `contractreset`.

    Best effort and non-fatal: the claim itself is already created, and failing to
    annotate it must not undo a selection the player successfully made.
    """
    try:
        _selection_ref(guild_id, week_key, user_id, mission_id).set(
            {"contract_id": str(contract_id)}, merge=True)
    except Exception as exc:  # noqa: BLE001 - annotation only; the claim still stands
        log.warning("Could not link weekly selection %s/%s/%s to contract %s: %s",
                    week_key, user_id, mission_id, contract_id, exc)


def _release_selection(guild_id: int, week_key: str, user_id: int, mission_id: int) -> None:
    """Undo a claim whose contract never got created, so the player can retry."""
    try:
        _selection_ref(guild_id, week_key, user_id, mission_id).delete()
    except Exception as exc:  # noqa: BLE001 - best effort; the claim is a stale row at worst
        log.warning("Could not release weekly selection %s/%s/%s: %s",
                    week_key, user_id, mission_id, exc)


# ── Embed builder ────────────────────────────────────────────────────────────

def _build_embed(guild_id: int, missions: list[dict], week_key: str) -> discord.Embed:
    now = datetime.now(TZ)
    start, end = _week_bounds(now)
    iso = now.isocalendar()

    embed = discord.Embed(
        title=t(guild_id, "wm.title"),
        description=t(guild_id, "wm.week",
                       n=iso[1],
                       start=start.strftime("%b %d"),
                       end=(end - timedelta(days=1)).strftime("%b %d, %Y")),
        color=discord.Color.from_rgb(30, 30, 30),
    )

    sym = settings.CURRENCY_SYMBOL
    tiers = [
        ("wm.easy", [m for m in missions if m["difficulty"] <= 3]),
        ("wm.medium", [m for m in missions if 4 <= m["difficulty"] <= 6]),
        ("wm.hard", [m for m in missions if 7 <= m["difficulty"] <= 8]),
        ("wm.extreme", [m for m in missions if m["difficulty"] >= 9]),
    ]

    for tier_key, tier_missions in tiers:
        if not tier_missions:
            continue
        lines = []
        for m in tier_missions:
            lines.append(f"**{m['id']}.** {m['desc_en']}\n　　`+{m['xp']} XP` · `+{m['coins']}` {sym}")
        embed.add_field(
            name=t(guild_id, tier_key),
            value="\n".join(lines),
            inline=False,
        )

    lockout = _week_bounds(now)[1] - timedelta(days=1)
    embed.add_field(
        name=t(guild_id, "wm.closes"),
        value=discord.utils.format_dt(lockout, style="F"),
        inline=False,
    )
    return embed


# ── Button View ──────────────────────────────────────────────────────────────

class MissionSelectView(discord.ui.View):
    """20 buttons, one per mission. Persistent via custom_ids."""
    def __init__(self, week_key: str = "", guild_id: int = 0, missions: list[dict] | None = None):
        super().__init__(timeout=None)
        self.week_key = week_key
        self.gid = guild_id
        if missions:
            for m in missions:
                style = (discord.ButtonStyle.green if m["difficulty"] <= 3
                         else discord.ButtonStyle.blurple if m["difficulty"] <= 6
                         else discord.ButtonStyle.red if m["difficulty"] <= 8
                         else discord.ButtonStyle.grey)
                btn = discord.ui.Button(
                    label=str(m["id"]),
                    style=style,
                    custom_id=f"wm:{week_key}:{guild_id}:{m['id']}",
                    row=min((m["id"] - 1) // 5, 4),
                )
                btn.callback = self._make_callback(m)
                self.add_item(btn)

    def _make_callback(self, mission: dict):
        async def callback(interaction: discord.Interaction):
            await _handle_selection(interaction, self.week_key, self.gid, mission)
        return callback


async def _handle_selection(interaction: discord.Interaction, week_key: str, guild_id: int, mission: dict):
    await interaction.response.defer(ephemeral=True)
    # The ACCOUNT id, not the raw snowflake. The claim, the wallet and the contract
    # are all keyed on the account, and for a player who linked Discord onto a
    # website account they already had, the two differ — so keying this surface on
    # the snowflake minted a *second* claim document for a mission the API surface
    # had already claimed, and paid the weekly coins and XP twice. Same correction
    # /linkcode, /linkas and /deletemydata already carry.
    from data import accounts as _accounts
    uid = await asyncio.to_thread(_accounts.account_for_discord, interaction.user.id)
    if not uid:
        await interaction.followup.send(
            t(guild_id, "wm.account_unavailable"), ephemeral=True)
        return

    # The issuer of a weekly mission is the bot itself, and every later stage
    # (auto-review, the payout, the fine) recognises it by that id. Before the
    # gateway hands us a user object there is no id to write, and a contract
    # issued by nobody is unreviewable, unpayable and still fineable — so refuse
    # rather than mint one. Structurally unreachable from a Discord interaction
    # (which cannot arrive before READY); said here because the API twin,
    # api_server.select_mission, IS reachable that early.
    bot_user = interaction.client.user
    if bot_user is None:
        await interaction.followup.send(t(guild_id, "wm.starting_up"), ephemeral=True)
        return

    # Locked?
    if _is_locked():
        is_exempt = False
        if getattr(settings, "WEEKLY_MISSIONS_MODS_IGNORE_LOCK", False):
            from cogs.gkchannels import is_mod
            from cogs.perms import real_user
            ru = real_user(interaction)   # mimic-safe: gate on the real invoker
            if isinstance(ru, discord.Member) and is_mod(ru):
                is_exempt = True
        if not is_exempt:
            await interaction.followup.send(t(guild_id, "wm.locked"), ephemeral=True)
            return

    # Has corp?
    corp = _get_corp(guild_id, uid)
    if not corp:
        await interaction.followup.send(t(guild_id, "wm.no_corp"), ephemeral=True)
        return

    # Already selected? The read is the friendly answer; the claim below is the gate.
    if _has_selected(guild_id, week_key, uid, mission["id"]):
        await interaction.followup.send(t(guild_id, "wm.already"), ephemeral=True)
        return

    # Selecting a weekly mission is one of the three ways to become the contractor
    # of an ACTIVE contract, and contract_actions.contractor_gate is where that rule
    # is stated. The mod's Select applies it; this button did not, which made the
    # debt cap and the active-contract cap advisory for anyone with Discord open.
    # Called on the loop, not in a thread: the gate reads `store`, which is only
    # ever read on the loop (see cogs.auctions.bid_refusal, which says the same).
    from contract_actions import contractor_gate
    if refusal := contractor_gate(guild_id, uid):
        await interaction.followup.send(refusal, ephemeral=True)
        return

    # Post contract in corp channel
    corp_channel_id = int(corp["channel_id"])
    channel = interaction.client.get_channel(corp_channel_id)
    if not channel:
        try:
            channel = await interaction.client.fetch_channel(corp_channel_id)
        except Exception:
            await interaction.followup.send("❌ Corp channel not found.", ephemeral=True)
            return

    # Claim the selection BEFORE creating the contract: everything above awaited
    # Discord, and two presses of the button that both passed `_has_selected`
    # would otherwise each create a contract for one mission.
    if _save_selection(guild_id, week_key, uid, mission["id"]) is False:
        await interaction.followup.send(t(guild_id, "wm.already"), ephemeral=True)
        return

    desc = mission["desc_en"]
    sym = settings.CURRENCY_SYMBOL

    # Create contract in Firestore
    from data import contracts as cdb
    now = datetime.now(TZ)
    _, week_end = _week_bounds(now)
    due = (week_end - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        c = cdb.create_contract(
            guild_id=guild_id,
            issuer_id=bot_user.id,
            issuer_name="Boundless Missions",
            contractor_id=uid,
            contractor_name=interaction.user.display_name,
            mission=desc,
            payment=mission["coins"],
            fine=mission["fine"],
            due_date=due,
        )
    except Exception:
        _release_selection(guild_id, week_key, uid, mission["id"])
        raise

    link_selection_contract(guild_id, week_key, uid, mission["id"], c["contract_id"])

    # Build embed for corp channel
    embed = discord.Embed(
        title=t(guild_id, "wm.contract_title", n=mission["id"]),
        description=desc,
        color=discord.Color.gold(),
    )
    embed.add_field(name="⭐", value=f"**{mission['difficulty']}/10**", inline=True)
    embed.add_field(name="💰", value=f"+{mission['coins']} {sym}", inline=True)
    embed.add_field(name="✨ XP", value=f"+{mission['xp']}", inline=True)
    embed.add_field(name="⚠️ Fine", value=f"{mission['fine']} {sym}", inline=True)
    embed.add_field(name="📅 Due", value=due, inline=True)
    embed.set_footer(text=f"Contract: {c['contract_id']}")
    embed.add_field(name="📤 Submitting", value=SUBMIT_IN_KSP, inline=False)

    from cogs.contract_views import ContractWorkView
    view = ContractWorkView(c["contract_id"], guild_id)
    # The post and the activation are inside the rollback too.
    #
    # Moving the bot-id check above the claim fixed the claim being taken and handed
    # back, but everything from here down still sat outside any `try`. A Discord
    # failure at `channel.send` — a permission change, a 500, the channel deleted
    # between the lookup and the post — left the week's claim held AND a bot-issued
    # contract stranded in PENDING that nobody could see or act on, for the rest of
    # the week, with the deferred interaction never answered.
    #
    # The contract is CANCELLED as well as the claim released: releasing alone would
    # leave the player able to re-select while the first, invisible contract still
    # counted against `contractor_gate`'s active-contract cap. Cancelled rather than
    # deleted because that is the terminal state the rest of the system already
    # understands, and it leaves the record behind to explain itself. No refund is
    # involved — a weekly mission is bot-issued and escrows nothing.
    try:
        msg = await channel.send(embed=embed, view=view)
        cdb.update_contract(guild_id, c["contract_id"], dm_message_id=str(msg.id), status=cdb.ACTIVE)
    except Exception:
        _release_selection(guild_id, week_key, uid, mission["id"])
        try:
            cdb.update_contract(guild_id, c["contract_id"], status=cdb.CANCELLED)
        except Exception:         # pragma: no cover - best effort; the claim is the thing
            log.exception("Could not stand down the stranded weekly contract %s",
                          c["contract_id"])
        raise

    await interaction.followup.send(
        t(guild_id, "wm.accepted", n=mission["id"], channel=channel.mention),
        ephemeral=True,
    )
    log.info("%s accepted weekly mission #%d (%s)", interaction.user, mission["id"], desc[:40])


# ── Custom Mission View ──────────────────────────────────────────────────────

class CustomMissionAcceptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept Custom Mission", style=discord.ButtonStyle.green, custom_id="cm:accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        # The ACCOUNT id, not the raw snowflake — the same correction
        # `_handle_selection` above carries, and for the same reason: the claim,
        # the wallet and the contract are all keyed on the account, and for a
        # player who linked Discord onto a website account they already had the
        # two differ. Keyed on the snowflake, the contract is written against an
        # id the mod is not authenticated as, so it can never be submitted — it
        # goes overdue into dispute and fines a wallet the game never reads.
        from data import accounts as _accounts
        uid = await asyncio.to_thread(_accounts.account_for_discord, interaction.user.id)
        if not uid:
            await interaction.followup.send(
                t(guild_id, "wm.account_unavailable"), ephemeral=True)
            return

        # See `_handle_selection`: a bot-issued contract with no issuer id is
        # unreviewable, unpayable and still fineable.
        bot_user = interaction.client.user
        if bot_user is None:
            await interaction.followup.send(t(guild_id, "wm.starting_up"), ephemeral=True)
            return

        corp = _get_corp(guild_id, uid)
        if not corp:
            await interaction.followup.send(t(guild_id, "wm.no_corp"), ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        
        # Check expiration
        footer_text = embed.footer.text or ""
        parts = dict(p.split(":") for p in footer_text.split("|") if ":" in p)
        expires = int(parts.get("expires", "0"))
        duration_days = int(parts.get("duration_days", "7"))
        
        if datetime.now(TZ).timestamp() > expires:
            await interaction.followup.send("❌ This custom mission has expired.", ephemeral=True)
            return
            
        msg_id = interaction.message.id
        # The read is the friendly answer; the claim below is the gate.
        if _has_selected(guild_id, "custom", uid, msg_id):
            await interaction.followup.send(t(guild_id, "wm.already"), ephemeral=True)
            return

        # Same gate the weekly button and the mod's Select apply: see
        # contract_actions.contractor_gate. On the loop, because it reads `store`.
        from contract_actions import contractor_gate
        if refusal := contractor_gate(guild_id, uid):
            await interaction.followup.send(refusal, ephemeral=True)
            return

        corp_channel_id = int(corp["channel_id"])
        channel = interaction.client.get_channel(corp_channel_id)
        if not channel:
            try:
                channel = await interaction.client.fetch_channel(corp_channel_id)
            except Exception:
                await interaction.followup.send("❌ Corp channel not found.", ephemeral=True)
                return

        # Claim the selection BEFORE creating the contract — the weekly path above
        # does the same and says why: everything up to here awaited Discord, and two
        # presses of the button that both passed `_has_selected` would otherwise each
        # create a paying, bot-issued contract for one mission. Written after the
        # fact (as this used to be) the claim is a note, not a gate.
        if _save_selection(guild_id, "custom", uid, msg_id) is False:
            await interaction.followup.send(t(guild_id, "wm.already"), ephemeral=True)
            return
        
        # Everything from here to the created contract is inside the claim, so any
        # failure hands it back — otherwise a mission nobody holds is one nobody
        # can ever take.
        import re
        from data import contracts as cdb
        try:
            coins = 0
            fine = 0
            xp = 0
            for field in embed.fields:
                if "💰" in field.name:
                    m = re.search(r'\+(\d+)', field.value)
                    if m: coins = int(m.group(1))
                elif "XP" in field.name:
                    m = re.search(r'\+(\d+)', field.value)
                    if m: xp = int(m.group(1))
                elif "Fine" in field.name:
                    m = re.search(r'(\d+)', field.value)
                    if m: fine = int(m.group(1))

            desc = embed.description

            now = datetime.now(TZ)
            due = (now + timedelta(days=duration_days)).strftime("%Y-%m-%d")

            c = cdb.create_contract(
                guild_id=guild_id,
                issuer_id=bot_user.id,
                issuer_name="Boundless Missions",
                contractor_id=uid,
                contractor_name=interaction.user.display_name,
                mission=desc,
                payment=coins,
                fine=fine,
                due_date=due,
            )
        except Exception:
            _release_selection(guild_id, "custom", uid, msg_id)
            raise
        
        sym = settings.CURRENCY_SYMBOL
        c_embed = discord.Embed(
            title="🎯 Custom Mission",
            description=desc,
            color=discord.Color.gold(),
        )
        c_embed.add_field(name="💰", value=f"+{coins} {sym}", inline=True)
        c_embed.add_field(name="✨ XP", value=f"+{xp}", inline=True)
        c_embed.add_field(name="⚠️ Fine", value=f"{fine} {sym}", inline=True)
        c_embed.add_field(name="📅 Due", value=due, inline=True)
        c_embed.set_footer(text=f"Contract: {c['contract_id']}")
        c_embed.add_field(name="📤 Submitting", value=SUBMIT_IN_KSP, inline=False)

        from cogs.contract_views import ContractWorkView
        view = ContractWorkView(c["contract_id"], guild_id)
        # The claim is deliberately NOT released if the send below fails: the
        # contract already exists and the player can accept it from the mod, so
        # handing the claim back would let them mint a second one for the same
        # mission — the very thing claiming first prevents.
        msg = await channel.send(embed=c_embed, view=view)
        cdb.update_contract(guild_id, c["contract_id"], dm_message_id=str(msg.id), status=cdb.ACTIVE)

        await interaction.followup.send(
            t(guild_id, "wm.custom_accepted", channel=channel.mention), ephemeral=True)


# ── Cog ──────────────────────────────────────────────────────────────────────

class WeeklyMissions(commands.Cog, name="WeeklyMissions"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._current_week = ""
        self._missions: list[dict] = []
        # guild_id -> week_key already posted/refreshed this run, so the 30-min
        # loop doesn't re-hit Firestore for every guild once the board is up.
        self._ensured: dict[int, str] = {}

    async def cog_load(self):
        # One-shot, idempotent: carry this week's claims over from the per-guild
        # collection the claim used to live in. See migrate_guild_selections.
        await asyncio.to_thread(migrate_guild_selections)
        self.refresh_loop.start()
        self.bot.add_view(CustomMissionAcceptView())

    async def cog_unload(self):
        self.refresh_loop.cancel()

    @tasks.loop(minutes=30)
    async def refresh_loop(self):
        """Check if we need to post/update the weekly embed."""
        await self._ensure_embed()

    @refresh_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _cleanup_old_missions(self, channel: discord.TextChannel, current_msg_id: int):
        try:
            async for message in channel.history(limit=50):
                if message.id == current_msg_id:
                    continue
                if message.author.id == self.bot.user.id and message.embeds:
                    embed = message.embeds[0]
                    if embed.title and ("Haftalık Görevler" in embed.title or "Weekly Missions" in embed.title):
                        await message.delete()
                        log.info("Deleted old weekly mission embed %d", message.id)
        except Exception as e:
            log.error("Failed to cleanup old missions: %s", e)

    async def _ensure_embed(self):
        # Post/refresh the weekly board in EVERY guild that has a weekly_missions
        # channel configured (multi-server). Missions are generated once per week
        # (deterministic from the week key) and stored per guild.
        week_key = _week_key()
        for guild in self.bot.guilds:
            channel = guild_config.resolve_channel(self.bot, guild.id, "weekly_missions")
            if channel is None:
                continue
            guild_id = guild.id

            if self._ensured.get(guild_id) == week_key:
                continue  # already posted/refreshed this guild for this week

            # Check Firestore for existing missions this week (per guild)
            missions, msg_id = _load_missions(guild_id, week_key)
            if not missions:
                missions = _generate_missions(week_key, settings.WEEKLY_MISSIONS_COUNT)
                log.info("Generated %d weekly missions for %s (guild %s)",
                         len(missions), week_key, guild_id)

            self._missions = missions
            self._current_week = week_key

            embed = _build_embed(guild_id, missions, week_key)
            view = MissionSelectView(week_key, guild_id, missions)

            # Try to edit existing message (no re-ping on edits)
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.edit(embed=embed, view=view)
                    log.info("Updated weekly missions embed (msg %d, guild %s)", msg_id, guild_id)
                    await self._cleanup_old_missions(channel, current_msg_id=msg_id)
                    self._ensured[guild_id] = week_key
                    continue
                except discord.NotFound:
                    pass

            # Post new message — ping the guild's notification role if mapped.
            notif = guild_config.resolve_role(guild, "notifications")
            content = notif.mention if notif else None
            msg = await channel.send(
                content=content, embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            _save_missions(guild_id, week_key, missions, msg.id)
            log.info("Posted weekly missions embed (msg %d, guild %s)", msg.id, guild_id)
            await self._cleanup_old_missions(channel, current_msg_id=msg.id)
            self._ensured[guild_id] = week_key

    @app_commands.command(name="add_custom_mission", description="Add an additional custom mission (Mod Only)")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(
        desc_en="Description in English",
        desc_tr="Description in Turkish",
        xp="XP reward",
        coins="Coin reward",
        fine="Fine if failed",
        accept_hours="Hours until contract accepting expires",
        duration_days="Days to complete the contract once accepted"
    )
    async def add_custom_mission(
        self, interaction: discord.Interaction,
        desc_en: str, desc_tr: str,
        xp: app_commands.Range[int, 0, 100_000],
        coins: app_commands.Range[int, 0, 10_000_000],
        fine: app_commands.Range[int, 0, 10_000_000],
        accept_hours: app_commands.Range[int, 1, 8760],
        duration_days: app_commands.Range[int, 1, 365],
    ):
        from cogs.perms import real_user
        ru = real_user(interaction)   # mimic-safe: gate on the real invoker
        if isinstance(ru, discord.Member):
            if not (ru.guild_permissions.kick_members or ru.guild_permissions.administrator):
                await interaction.response.send_message("❌ Mod only.", ephemeral=True)
                return

        # The same fine cap every other contract-creation path enforces.
        #
        # This was the one route into an ACTIVE contract that applied none of them:
        # the fields were bare `int`s with no range, no floor and no cross-check, and
        # the accept handler calls `cdb.create_contract` directly. So a moderator
        # could post a card that looked exactly like the weekly board offering
        # `coins:1, fine:500000` — and since an unpayable fine is no longer forgiven
        # but recorded as a garnished debt, accepting it followed the player
        # permanently and, past DEBT_MAX_OUTSTANDING, stopped them taking any
        # contract at all. `duration_days:0` additionally produced a due date at or
        # before today, so the daily sweep pushed it straight into dispute.
        from api_server import _fine_too_large
        if bad := _fine_too_large(coins, fine):
            await interaction.response.send_message(f"❌ {bad}", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🎯 Custom Mission / Özel Görev",
            description=f"**EN:** {desc_en}\n\n**TR:** {desc_tr}",
            color=discord.Color.purple(),
        )
        sym = settings.CURRENCY_SYMBOL
        embed.add_field(name="💰", value=f"+{coins} {sym}", inline=True)
        embed.add_field(name="✨ XP", value=f"+{xp}", inline=True)
        embed.add_field(name="⚠️ Fine", value=f"{fine} {sym}", inline=True)
        
        now = datetime.now(TZ)
        expires_at = now + timedelta(hours=accept_hours)
        embed.add_field(name="⏰ Accepts Until", value=discord.utils.format_dt(expires_at, style="F"), inline=False)
        embed.set_footer(text=f"duration_days:{duration_days}|expires:{int(expires_at.timestamp())}")

        view = CustomMissionAcceptView()

        channel = guild_config.resolve_channel(interaction.client, interaction.guild_id, "weekly_missions")
        if channel is None:
            await interaction.response.send_message(
                "❌ No weekly-missions channel is configured for this server. "
                "Set one with `/admin setchannel`.", ephemeral=True)
            return

        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Custom mission posted to mission-control.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyMissions(bot))
