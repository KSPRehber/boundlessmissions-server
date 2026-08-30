"""
cogs/targets.py – "who do you mean", for moderator commands that must be able to
reach a player who has no Discord account.

Every mod tool in this bot was written when a player was a Discord member and
nothing else, so each one takes a `discord.Member` and spends `member.id`. That
stopped being the whole population the moment `data/accounts.py` gave an account
an id of its own: someone who signed up on the website has a wallet, XP, contracts
and listings, and **no snowflake to type into a slash command**. A moderator
fielding "my balance is wrong" for such a player had no command that could touch
them — the field they needed to fill simply did not exist.

So the target of a mod command is now one of two fields, and this module is the
one place that turns either into the thing the store actually keys on:

    /b givemoney member:@jeb        →  account id "1902…"
    /b givemoney username:jebediah  →  account id "a_9fK…"

Three things here are load-bearing.

**The member path resolves too.** It would be easier to take `member.id` when a
member was given and only consult accounts for a username — and it would be
wrong. `account_for_discord` is the identity function for almost everybody, but
not for a player who linked their Discord *onto* an account they already had:
their snowflake carries an `account_discord` index row pointing somewhere else.
Spending `member.id` for them credits a wallet the game never reads, so the fine
lands nowhere and the player is right when they say nothing happened. Routing
both paths through the same resolver is what makes the Discord case correct, not
just the website one — and it is the reason this is a resolver and not a
`username or member.id` one-liner at each call site.

**A failed read is not an answer.** `data/accounts.py` returns None to mean
"unknowable", never "absent", because an absent index row means *identity* and
guessing it hands out somebody else's wallet. That contract only holds if callers
honour it, so every path here refuses on None rather than falling back to the
snowflake. `owner_of_username` exists for the same reason: a Firestore blip must
read as "try again", never as "no such player".

**Both fields filled is refused, not resolved.** Picking one (or worse, picking
whichever resolves) turns a moderator's slip into a silent action on the wrong
account. There is no ambiguity worth resolving here: they know which one they
meant, and asking costs one retry.

The autocomplete is deliberately thin — a document-id prefix scan capped at 25
reads, memoised for a minute — because it fires on every keystroke and the cost
guard counts every one of them (see `cost_guard.py`). A moderator command is rare
enough that a stale minute is free and a per-keystroke query is not.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord import app_commands

from data import accounts
from data.store import store

log = logging.getLogger(__name__)

# What the two fields are called, in the words the describe() strings use. Kept
# here so a command that adds the pair cannot describe it differently.
MEMBER_DESC = "Target Discord member"
USERNAME_DESC = "…or a Boundless username, for a player with no Discord account"


class TargetError(Exception):
    """Why a target could not be resolved, phrased for the moderator who typed it.

    Carries a sentence rather than a code because every caller does the same thing
    with it — shows it, ephemerally, and stops. The distinction that matters is
    already baked into the sentence: "no such name" invites a retype, "couldn't
    reach the account service" invites a wait.
    """


@dataclass(frozen=True)
class Target:
    """A resolved player: the id everything is keyed on, plus what to call them.

    `member` is None for a website-only account and for a Discord user who is not
    in this guild — the two are different situations but the same capability, so
    every caller must treat a missing member as "cannot mention, cannot DM, cannot
    read an avatar from" rather than as "not a real player".
    """
    account_id: str
    label: str
    member: discord.Member | None = None
    username: str = ""

    @property
    def mention(self) -> str:
        """Something safe to put in an embed. A website account has no mention and
        `<@a_9fK…>` is broken text, not a link — the same reasoning as the ticket
        embeds in `cogs/tickets.py`."""
        return self.member.mention if self.member else f"**{self.label}**"

    @property
    def avatar_url(self) -> str | None:
        return getattr(self.member.display_avatar, "url", None) if self.member else None

    @property
    def can_dm(self) -> bool:
        return self.member is not None

    async def dm(self, *args, **kwargs) -> bool:
        """Best effort. False covers both "no Discord to DM" and "DMs closed",
        because a caller can do nothing different about either — what it must not
        do is report an action as un-delivered when the action itself landed."""
        if self.member is None:
            return False
        try:
            await self.member.send(*args, **kwargs)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False


def _label_for(account_id: str, acct: dict | None,
               member: discord.Member | None) -> tuple[str, str]:
    """(label, username) — how this account is named in a moderator's confirmation.

    Preference order is the account's own identity first and Discord's second: the
    display name and username are what the player sees on the website and in the
    mod, so a confirmation using them can be checked against what the player
    reported. A guild nickname is a per-server alias and is the wrong thing to
    read back to somebody in a ticket.
    """
    acct = acct or {}
    username = str(acct.get("username") or "")
    shown = str(acct.get("display_name") or "")
    if not shown and member is not None:
        shown = member.display_name
    if username:
        return (f"{shown} (@{username})" if shown else f"@{username}"), username
    if shown:
        return shown, ""
    return f"`{account_id}`", ""


async def resolve(interaction: discord.Interaction,
                  member: discord.Member | None,
                  username: str | None,
                  *, default_self: bool = False) -> Target:
    """The player a command was aimed at. Raises `TargetError` with a sentence.

    `default_self` is for the look-up commands (`/balance`, `/rank`, `/rescues`)
    where naming nobody means "me". A command that *changes* something must leave
    it off: defaulting a `/setbalance` with a forgotten target to the moderator
    running it is the one mistake this module could make that nobody would notice.
    """
    name = (username or "").strip()

    if member is not None and name:
        raise TargetError(
            "Give **either** a member **or** a username — not both. "
            "I won't guess which one you meant.")

    if member is None and not name:
        if not default_self:
            raise TargetError(
                "Name someone: pick a `member`, or type their Boundless "
                "`username` if they have no Discord account.")
        member = interaction.user

    if member is not None:
        account_id = await asyncio.to_thread(accounts.account_for_discord, member.id)
        if account_id is None:
            raise TargetError(
                "Couldn't reach the account service to work out whose account "
                "that is. Nothing was changed — try again in a moment.")
        acct = await asyncio.to_thread(accounts.get_account, account_id)
        label, uname = _label_for(account_id, acct, member)
        return Target(account_id=account_id, label=label, member=member, username=uname)

    # A username, or — for a moderator who copied one out of the admin console —
    # an account id typed into the same field. The username is tried first and
    # wins outright: names are the thing players know, ids are the fallback, and a
    # name can never look like a snowflake anyway (`_USERNAME_RE` requires the
    # first character to be alphanumeric but the reserved-word list and the
    # 3-character floor do not exclude digits, so the id branch must come second).
    owner = await asyncio.to_thread(accounts.owner_of_username, name)
    if owner is None:
        raise TargetError(
            "Couldn't reach the account service to look that name up. "
            "Nothing was changed — try again in a moment.")

    if not owner:
        owner = await _account_id_if_exists(name)
        if owner is None:
            raise TargetError(
                "Couldn't reach the account service to look that up. "
                "Nothing was changed — try again in a moment.")
        if not owner:
            raise TargetError(
                f"No Boundless account is called **{discord.utils.escape_markdown(name)}**. "
                f"Usernames are what players choose on the website or in the mod — "
                f"they aren't Discord names.")

    acct = await asyncio.to_thread(accounts.get_account, owner)
    target_member = None
    if interaction.guild is not None:
        did = str((acct or {}).get("discord_id") or "")
        if not did and accounts.is_discord_account(owner):
            did = owner
        if did.isdigit():
            target_member = interaction.guild.get_member(int(did))
    label, uname = _label_for(owner, acct, target_member)
    _remember_name(owner, label)
    return Target(account_id=owner, label=label, member=target_member, username=uname)


async def _account_id_if_exists(candidate: str) -> str | None:
    """`candidate` if it is an account id with a document behind it, "" if not,
    None if that could not be determined.

    The existence check is the whole point: without it a typo'd id would be
    accepted as a target and `store.get_user` would mint an empty wallet for it,
    so the moderator's confirmation would report a successful adjustment to an
    account that has never existed. Same reasoning as the console's
    `admin_user_adjust`, which refuses ids the store has never seen.
    """
    cand = candidate.strip()
    if not (cand.isdigit() or cand.startswith(accounts.FIREBASE_PREFIX)):
        return ""
    try:
        acct = await asyncio.to_thread(accounts.get_account, cand)
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Target id probe %r failed: %s", cand, exc)
        return None
    if acct is not None:
        return cand
    # A Discord id with no account document is still a real target if the wallet
    # store has a record for it — every user who predates `data/accounts.py` is in
    # exactly that state, and refusing them here would take away the reach this
    # module exists to add.
    return cand if store.has_user(cand) else ""


# ── Autocomplete ─────────────────────────────────────────────────────────────
#
# Firestore reads are metered (see cost_guard.py) and an autocomplete callback
# fires per keystroke, so the query is capped at 25 document ids and its result is
# memoised per prefix. The TTL is short enough that a name claimed during a
# moderator's session shows up, and long enough that typing one costs one query.

_CACHE_TTL = 60.0
_CACHE_MAX = 256
_cache: dict[str, tuple[float, list[str]]] = {}


async def _names_for(prefix: str) -> list[str]:
    key = accounts.normalize_username(prefix)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    names = await asyncio.to_thread(accounts.search_usernames, key, 25)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()          # a moderator picker; an LRU would be ceremony
    _cache[key] = (now, names)
    return names


async def username_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Claimed usernames beginning with what has been typed.

    Never raises: an autocomplete that throws renders as a permanently empty
    picker with no explanation, which is worse than no autocomplete at all — the
    field still accepts free text, and `resolve` does the real lookup.
    """
    try:
        names = await _names_for(current)
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Username autocomplete failed for %r: %s", current, exc)
        return []
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


def username_param(fn):
    """Attach the username autocomplete to a command's `username` parameter."""
    return app_commands.autocomplete(username=username_autocomplete)(fn)


# ── Leaderboard names ────────────────────────────────────────────────────────
#
# A leaderboard reads its rows straight out of the wallet store, whose keys are
# account ids — so the moment a website-only player earns anything, every board
# in the bot hit `int(uid)` on an id like "a_9fK…" and raised ValueError, taking
# the whole command down rather than the one row. Naming those rows is the fix,
# and it has to be cheap: the boards render on demand and a Firestore read per row
# would put ten of them behind every `/leaderboard`.
#
# So the lookup is two caches and no blocking read. Snowflakes are answered from
# Discord's own member cache, which costs nothing; account ids are answered from a
# TTL'd name cache that `prefetch_names` fills in one hop before rendering. A name
# that is in neither degrades to the id rather than to an exception — the row is
# worth showing even when nobody can say whose it is.

_NAME_TTL = 300.0
_names: dict[str, tuple[float, str]] = {}


def _cached_name(account_id: str) -> str:
    hit = _names.get(str(account_id))
    return hit[1] if hit and time.time() - hit[0] < _NAME_TTL else ""


def _remember_name(account_id: str, name: str) -> None:
    if name:
        _names[str(account_id)] = (time.time(), name)


async def prefetch_names(account_ids) -> None:
    """Warm the name cache for the account ids a board is about to render.

    Only ids that are not Discord snowflakes are fetched — a snowflake is named
    from the member cache for free — and only ones not already cached, so a board
    refreshed twice in five minutes costs nothing the second time.
    """
    wanted = [str(i) for i in account_ids
              if not accounts.is_discord_account(i) and not _cached_name(i)]
    if not wanted:
        return

    def _read():
        out = {}
        for aid in wanted:
            try:
                acct = accounts.get_account(aid)
            except Exception:                      # pragma: no cover - defensive
                continue
            if not acct:
                continue
            label, _u = _label_for(aid, acct, None)
            out[aid] = label
        return out

    try:
        for aid, label in (await asyncio.to_thread(_read)).items():
            _remember_name(aid, label)
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Leaderboard name prefetch failed: %s", exc)


def board_name(guild: discord.Guild | None, account_id) -> str:
    """What to print for one leaderboard row. Never raises, never blocks."""
    aid = str(account_id)
    if aid.isdigit():
        member = guild.get_member(int(aid)) if guild is not None else None
        if member is not None:
            return member.display_name
    cached = _cached_name(aid)
    if cached:
        return cached
    return f"User {aid}"


async def reject(interaction: discord.Interaction, err: TargetError) -> None:
    """Show a resolution failure and stop.

    A command that has already deferred gets its *original* response edited, not a
    followup. The difference is visible: a deferred interaction is showing a
    "thinking…" placeholder that only the original response resolves, so a
    followup would leave that placeholder sitting there for good next to the
    error. Editing it also keeps the reply in whatever visibility the defer chose
    — an ephemeral defer stays ephemeral, which a followup's `ephemeral=` flag
    cannot be relied on to reproduce.
    """
    text = f"⚠️ {err}"
    if interaction.response.is_done():
        await interaction.edit_original_response(content=text)
    else:
        await interaction.response.send_message(text, ephemeral=True)
