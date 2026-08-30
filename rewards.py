"""
rewards.py – the one place XP is granted for something a player did.

XP used to arrive by two unrelated routes. Chatting went through `store.add_xp`,
which held the lock, respected a cooldown and returned `leveled_up` — so a
level-up paid `LEVEL_UP_REWARD` and got announced. Everything a player actually
*flew* went through `store.set_xp`, the admin setter, as a read-modify-write
whose read sat outside the lock: concurrent awards lost each other, and because
`set_xp` only recomputes `level` and reports nothing, crossing a level from a
completed contract paid no reward and told nobody.

Now that contracts are the main source rather than a side channel, both halves of
that had to go. `store.award_xp` is atomic and reports the level-up; this module
is what every award site calls so the reward and the announcement happen once, in
one place, the same way for a contract, a screenshot and a weekly mission.

Announcements go to the notification feed FIRST and to Discord only if the guild
has a level-up channel mapped. That order is deliberate: the feed is the surface
every player has, including the ones who never join a Discord server.
"""

import logging
import time

import settings
from data.store import store, xp_for_level

log = logging.getLogger(__name__)


def _api():
    """Late import of `api_server`.

    The notification hub and the live bot handle live there, and `api_server`
    imports the modules that call this one — importing it at module scope would
    be a cycle. By the time any function below runs both modules are loaded, so
    this is a dict lookup. Same pattern as `contract_actions._api`.
    """
    import api_server
    return api_server


def contract_xp(payment: int, *, bot_issued: bool = False) -> int:
    """XP for completing a contract worth `payment`.

    Difficulty is back-derived from the payout using the weekly-mission
    constants, which is what the in-game auto-accept path already did — kept
    verbatim so this change moves XP to one code path without also silently
    rebalancing what anything is worth.

    A player-issued contract (`bot_issued=False`, the default) earns XP only when
    `settings.CONTRACT_XP_HUMAN_ISSUED` is on. Its XP would scale with a price the
    issuer sets and the issuer alone judges the work, which makes two cooperating
    accounts an unbounded XP pump — and, via LEVEL_UP_REWARD, a mint. The default
    is the safe answer; the switch is where the balance decision lives.
    """
    if not bot_issued and not settings.CONTRACT_XP_HUMAN_ISSUED:
        return 0
    per_diff = settings.WEEKLY_COINS_PER_DIFFICULTY
    if per_diff <= 0:
        return 0
    return (int(payment) // per_diff) * settings.WEEKLY_XP_PER_DIFFICULTY


# Why human_contract_xp said no. "" means it paid.
XP_GATE_COOLDOWN = "cooldown"
XP_GATE_PAIR = "pair_limit"
XP_GATE_DAILY = "daily_limit"


def pair_completions(guild_id: int, contractor_id, issuer_id,
                     window_seconds: float) -> int:
    """Player-issued contracts completed between these two accounts in either
    direction inside the window. A grant is logged on the contractor's record
    with the issuer as peer, so the reverse direction lives on the other record."""
    a, b = str(contractor_id), str(issuer_id)
    return (sum(1 for e in store.contract_xp_log(guild_id, a, window_seconds)
                if str(e.get("peer")) == b)
            + sum(1 for e in store.contract_xp_log(guild_id, b, window_seconds)
                  if str(e.get("peer")) == a))


async def human_contract_xp(guild_id: int, contractor_id, issuer_id, payment: int,
                            *, now: float | None = None) -> tuple[int, str, bool]:
    """The XP a contractor earns from a *player-issued* contract, gated.

    Returns (xp, gate, flag_pair): `gate` is "" when XP was paid, else the brake
    that stopped it; `flag_pair` is True on exactly the completion that crosses
    the pair limit, so the caller can raise one moderator flag rather than one
    per cycle. Three brakes, in order (see settings.py, "XP from player-issued
    contracts"): the per-contract cap, the per-contractor cooldown, and the
    per-pair daily count. Coins are never touched here — the deal settles as
    agreed whatever this says — and the completion is recorded either way, so a
    pair that keeps cycling under the cooldown still counts toward its limit.

    Deterministic on purpose: the evidence of a mint (same two accounts, both
    directions, inside a day) is structural, and nothing an AI could read out of
    the mission text tells it apart from two friends who genuinely build for each
    other. That case is what the moderator flag is for.
    """
    now = time.time() if now is None else now
    window = max(0, settings.CONTRACT_PAIR_WINDOW_HOURS) * 3600.0
    xp = min(contract_xp(payment), max(0, settings.CONTRACT_XP_HUMAN_MAX))
    cooldown = max(0, settings.CONTRACT_XP_COOLDOWN_MINUTES) * 60.0

    # The cooldown/daily/pair reads and the completion write MUST be one atomic
    # step, or two concurrent reviews for a colluding pair could both read the
    # pre-write state and both pass the gate (an XP + level-up-coin mint). The
    # decision policy stays here; store.claim_contract_xp holds it under one lock.
    return await store.claim_contract_xp(
        guild_id, contractor_id, str(issuer_id),
        candidate_xp=xp,
        cooldown_seconds=cooldown,
        daily_max=settings.CONTRACT_XP_HUMAN_DAILY_MAX,
        pair_free=settings.CONTRACT_PAIR_XP_FREE_PER_DAY,
        window_seconds=window,
        now=now)


async def grant_xp(guild_id: int, user_id: int, amount: int, *,
                   reason: str = "") -> tuple[int, bool]:
    """Award `amount` XP and settle any level-up. Returns (awarded, leveled_up).

    `reason` is a short human phrase naming what earned it ("Contract completed")
    and is shown in the level-up notification. A non-positive amount is a no-op,
    so callers can pass a computed value without guarding it first.
    """
    if amount <= 0:
        return 0, False

    new_xp, new_level, leveled_up = await store.award_xp(guild_id, user_id, amount)
    if not leveled_up:
        return amount, False

    reward = settings.LEVEL_UP_REWARD
    new_balance = (await store.add_balance(
                       guild_id, user_id, reward, garnishable=True,
                       category=store.TX_REWARD,
                       detail="Reached level %d" % new_level)
                   if reward else 0)

    _notify_level_up(guild_id, user_id, new_level, new_xp, reward, new_balance, reason)
    await _announce_level_up(guild_id, user_id, new_level, new_xp, reward, new_balance)

    log.info("User %s reached level %d (+%d XP%s, +%d level-up reward)",
             user_id, new_level, amount, f", {reason}" if reason else "", reward)
    return amount, True


def _notify_level_up(guild_id: int, user_id: int, level: int, total_xp: int,
                     reward: int, balance: int, reason: str) -> None:
    """Put the level-up in the player's own feed. Best-effort: a feed write must
    never roll back XP that has already been banked."""
    try:
        body = f"You reached **Level {level}**."
        if reason:
            body = f"{reason} — {body}"
        body += (f"\nTotal XP: {total_xp:,} · Next level: "
                 f"{xp_for_level(level + 1):,} XP")
        if reward:
            body += (f"\n+{reward:,} {settings.CURRENCY_NAME} "
                     f"(balance: {balance:,})")
        _api()._create_notification(
            guild_id, user_id, "level_up", "🚀 Level Up!", body,
            {"level": level, "xp": total_xp, "reward": reward},
        )
    except Exception as exc:
        log.warning("Could not write level-up notification for %s: %s", user_id, exc)


async def _announce_level_up(guild_id: int, user_id: int, level: int,
                             total_xp: int, reward: int, balance: int) -> None:
    """Post the level-up to the guild's level-up channel, when it has one.

    Entirely optional and best-effort. With message XP gone there is no longer a
    channel the player was "just talking in" to fall back to, so an unmapped
    `level_up` channel means no Discord post at all — the feed already carried it.
    """
    if not settings.ANNOUNCE_LEVEL_UP:
        return
    try:
        import discord
        from data import guild_config
        from i18n import t

        bot = _api()._bot_instance
        if bot is None:
            return
        channel = guild_config.resolve_channel(bot, guild_id, "level_up")
        if channel is None:
            return

        # A mention only resolves for an account that IS a Discord account. Once
        # sign-ups no longer go through Discord a user id may not be a snowflake,
        # and `<@a_xxxx>` would render as broken text — so fall back to the stored
        # username. Same test as api_server's admin-user lookup.
        uid = str(user_id)
        if uid.isdigit():
            who = f"<@{uid}>"
        else:
            who = store.get_user(guild_id, user_id).get("username") or "A player"

        embed = discord.Embed(
            title=t(guild_id, "xp.level_up.title"),
            description=t(guild_id, "xp.level_up.desc",
                          user=who, level=level, xp=f"{total_xp:,}",
                          next_xp=f"{xp_for_level(level + 1):,}",
                          symbol=settings.CURRENCY_SYMBOL, reward=f"{reward:,}",
                          currency=settings.CURRENCY_NAME, balance=f"{balance:,}"),
            color=discord.Color.gold(),
        )
        await channel.send(embed=embed)
    except Exception as exc:
        log.warning("Could not announce level-up for %s: %s", user_id, exc)
