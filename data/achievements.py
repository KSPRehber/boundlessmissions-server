"""
data/achievements.py – Global, cross-server KSP achievement levels.

KSP achievement "levels" (Kerbin Orbit, Mun Landing, …) are earned by a player,
not by a player-in-a-server. To make them usable across every guild the bot is in
— each of which has its own role IDs — the unlocked set is stored GLOBALLY per
user, keyed only by Discord user id:

    ksp_achievements/{user_id} → { "unlocked_levels": [int, ...] }

cogs/roles.py maps this global set onto whichever role IDs the *current* guild has
mapped (see data/guild_config.py). Legacy per-guild `unlocked_levels` written by
data/store.py are migrated into the global doc the first time a user is read.
"""

from __future__ import annotations

import logging

from data.store import _db, store

log = logging.getLogger(__name__)

_COLLECTION = "ksp_achievements"

# user_id (str) -> set of unlocked level ints
_cache: dict[str, set[int]] = {}


def _doc(user_id: int):
    return _db.collection(_COLLECTION).document(str(user_id))


def _legacy_levels(user_id: int) -> set[int]:
    """Union any unlocked_levels already in the (global) store for this user, so
    existing players keep what they earned before the achievement store existed."""
    found: set[int] = set()
    rec = store._users.get(str(user_id))
    if rec:
        for lvl in rec.get("unlocked_levels", []) or []:
            found.add(int(lvl))
        old_max = rec.get("max_unlocked_level", 0) or 0
        if old_max > 0:
            found.add(int(old_max))
    return found


def get_unlocked(user_id: int) -> set[int]:
    """Return the user's global set of unlocked level ints (cached). On first
    access, reads Firestore and, if absent, migrates legacy per-guild data."""
    uid = str(user_id)
    if uid in _cache:
        return set(_cache[uid])

    levels: set[int] = set()
    migrated = False
    try:
        snap = _doc(user_id).get()
        if snap.exists:
            data = snap.to_dict() or {}
            levels = {int(x) for x in (data.get("unlocked_levels") or [])}
        else:
            levels = _legacy_levels(user_id)
            migrated = bool(levels)
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to read achievements for %s: %s", user_id, exc)
        levels = _legacy_levels(user_id)

    _cache[uid] = set(levels)
    if migrated:
        _write(user_id)
        log.info("Migrated %d legacy achievement levels for user %s", len(levels), uid)
    return set(levels)


def add_unlocked(user_id: int, level: int) -> bool:
    """Add a level to the user's global unlocked set. Returns True if newly added."""
    current = get_unlocked(user_id)
    if level in current:
        return False
    current.add(int(level))
    _cache[str(user_id)] = current
    _write(user_id)
    return True


def remove_unlocked(user_id: int, level: int) -> bool:
    """Remove a level (level=0 clears all). Returns True if anything changed."""
    current = get_unlocked(user_id)
    if level == 0:
        if not current:
            return False
        _cache[str(user_id)] = set()
        _write(user_id)
        return True
    if level not in current:
        return False
    current.discard(int(level))
    _cache[str(user_id)] = current
    _write(user_id)
    return True


def _write(user_id: int) -> None:
    levels = sorted(_cache.get(str(user_id), set()))
    try:
        _doc(user_id).set({"unlocked_levels": levels})
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to write achievements for %s: %s", user_id, exc)
