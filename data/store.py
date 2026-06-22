"""
data/store.py – Firestore-backed persistent user data store.

Keeps all user data in memory for fast access (every message triggers XP),
syncs to Firestore periodically and on shutdown.

Firestore structure:
    guilds/{guild_id}/users/{user_id} → { xp, level, balance, messages, ... }

User record schema:
{
    "xp": int,
    "level": int,
    "balance": int,
    "messages": int,
    "last_xp_time": float (unix timestamp),
    "joined_at": str (ISO 8601),
}
"""

import asyncio
import logging
import os
import time
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage

import settings
from config import cfg

log = logging.getLogger(__name__)

# Type alias
UserData = dict[str, Any]

# ── Firebase init ────────────────────────────────────────────────────────────
_cred = credentials.Certificate(cfg.FIREBASE_CREDENTIALS)
_bucket_name = os.getenv("FIREBASE_STORAGE_BUCKET", "")
_app = firebase_admin.initialize_app(_cred, {
    "storageBucket": _bucket_name,
} if _bucket_name else None)
from data.firebase_guard import wrap_firestore, wrap_bucket

# All Firestore / Storage access flows through these two handles (every cog
# imports them from here), so wrapping them is enough to meter spend and enforce
# the Firebase budget cap project-wide. See cost_guard.py / firebase_guard.py.
_db = wrap_firestore(firestore.client())
_storage_bucket = wrap_bucket(fb_storage.bucket() if _bucket_name else None)
if _storage_bucket:
    log.info("Firebase Storage configured: %s", _bucket_name)
else:
    log.warning("FIREBASE_STORAGE_BUCKET not set — contract file uploads disabled")


# ── Upload sanitization (client-supplied filenames / content types) ──────────
#
# Client-supplied filenames flow into Firebase Storage object paths
# (contracts/{id}/{filename} etc.). GCS treats the object name literally, so a
# name with "/" or ".." can't traverse out of its prefix, but it CAN collide with
# or shadow a sibling object and lets the client control the public object name.
# safe_filename reduces any name to a single safe basename. safe_content_type
# stops a client from having its public blob served as active content (HTML/SVG/JS).

import re as _re

_SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")

_SAFE_UPLOAD_CTYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/gzip", "application/octet-stream", "text/plain",
}


def safe_filename(name: str, default: str = "file") -> str:
    """Reduce a client-supplied filename to a safe storage basename.

    Strips any directory components (so it can't escape its prefix or shadow a
    sibling via '..'/slashes), replaces anything outside [A-Za-z0-9._-], drops
    leading dots (so '..' / '.env' can't become hidden/dot names), and caps the
    length. Falls back to `default` when nothing usable remains."""
    name = (name or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1]          # basename only
    name = _SAFE_NAME_RE.sub("_", name)
    name = name.lstrip(".")                 # ".." -> "", ".craft" -> "craft"
    name = name[:128]
    return name or default


def safe_content_type(claimed: str) -> str:
    """Clamp a client-claimed content type to an inert allowlist. Anything not
    explicitly safe (text/html, image/svg+xml, application/javascript, …) becomes
    application/octet-stream, so a public blob can't be served as active content."""
    c = (claimed or "").split(";", 1)[0].strip().lower()
    return c if c in _SAFE_UPLOAD_CTYPES else "application/octet-stream"


def _default_user() -> UserData:
    """Return a fresh user record with default values."""
    return {
        "user_id": "",
        "username": "",
        "language": "",
        "xp": 0,
        "level": 0,
        "balance": settings.STARTING_BALANCE,
        "messages": 0,
        "last_xp_time": 0.0,
        "joined_at": "",
        "unlocked_levels": [],
        "rescues": 0,
    }


def xp_for_level(level: int) -> int:
    """Calculate total XP needed to reach a given level."""
    if level <= 0:
        return 0
    return int(settings.LEVEL_XP_BASE * (level ** settings.LEVEL_XP_EXPONENT))


def level_from_xp(xp: int) -> int:
    """Derive the current level from total XP."""
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


class UserStore:
    """In-memory store backed by Firestore."""

    def __init__(self) -> None:
        # guild_id (str) -> user_id (str) -> UserData
        self._data: dict[str, dict[str, UserData]] = {}
        self._lock = asyncio.Lock()
        self._dirty_users: set[tuple[str, str]] = set()  # (guild_id, user_id) pairs

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def load(self) -> None:
        """Load all guild/user data from Firestore into memory."""
        total = 0
        try:
            guilds_ref = _db.collection("guilds")
            for guild_doc in guilds_ref.stream():
                guild_id = guild_doc.id
                self._data[guild_id] = {}
                users_ref = guilds_ref.document(guild_id).collection("users")
                for user_doc in users_ref.stream():
                    user_data = user_doc.to_dict()
                    # Merge with defaults to handle schema evolution
                    merged = _default_user()
                    merged.update(user_data)
                    self._data[guild_id][user_doc.id] = merged
                    total += 1
            log.info("Loaded %d user records from Firestore", total)
        except Exception as exc:
            log.error("Failed to load from Firestore: %s — starting fresh", exc)
            self._data = {}

    async def save(self) -> None:
        """Flush all dirty user records to Firestore."""
        async with self._lock:
            if not self._dirty_users:
                return
            dirty = list(self._dirty_users)
            self._dirty_users.clear()

        try:
            # Collect which guilds were touched so we create parent docs
            touched_guilds: set[str] = set()
            batch = _db.batch()
            count = 0

            for guild_id, user_id in dirty:
                guild_data = self._data.get(guild_id, {})
                user_data = guild_data.get(user_id)
                if user_data is None:
                    continue

                touched_guilds.add(guild_id)
                doc_ref = (
                    _db.collection("guilds")
                    .document(guild_id)
                    .collection("users")
                    .document(user_id)
                )
                batch.set(doc_ref, user_data)
                count += 1

                # Firestore batches max out at 500 operations
                if count >= 450:
                    batch.commit()
                    log.info("Committed Firestore batch (%d docs)", count)
                    batch = _db.batch()
                    count = 0

            # Ensure guild parent documents exist so load() can discover them
            for guild_id in touched_guilds:
                guild_ref = _db.collection("guilds").document(guild_id)
                batch.set(guild_ref, {"_exists": True}, merge=True)
                count += 1

            if count > 0:
                batch.commit()
            log.info("Saved %d user records to Firestore", len(dirty))
        except Exception as exc:
            log.error("Failed to save to Firestore: %s", exc, exc_info=True)
            # Re-add to dirty so we retry next cycle
            async with self._lock:
                self._dirty_users.update(dirty)

    async def save_if_dirty(self) -> None:
        """Save only if data has changed since last save."""
        if self._dirty_users:
            await self.save()

    def _mark_dirty(self, guild_id: int, user_id: int) -> None:
        """Mark a user record as needing a Firestore write."""
        self._dirty_users.add((str(guild_id), str(user_id)))

    # ── User access ──────────────────────────────────────────────────────────

    def _guild(self, guild_id: int) -> dict[str, UserData]:
        """Get or create the guild bucket."""
        key = str(guild_id)
        if key not in self._data:
            self._data[key] = {}
        return self._data[key]

    def get_user(self, guild_id: int, user_id: int) -> UserData:
        """Get a user's record, creating a default one if needed."""
        guild = self._guild(guild_id)
        key = str(user_id)
        if key not in guild:
            guild[key] = _default_user()
            self._mark_dirty(guild_id, user_id)
        return guild[key]

    def get_all_users(self, guild_id: int) -> dict[str, UserData]:
        """Get all user records for a guild."""
        return self._guild(guild_id)

    async def delete_user(self, guild_id: int, user_id: int) -> bool:
        """Erase a user's profile record from memory and Firestore. Used by the
        user-initiated 'delete my data' flow. Returns True if a record existed."""
        gkey, ukey = str(guild_id), str(user_id)
        async with self._lock:
            existed = self._data.get(gkey, {}).pop(ukey, None) is not None
            self._dirty_users.discard((gkey, ukey))  # don't let a pending write resurrect it
        try:
            _db.collection("guilds").document(gkey).collection("users").document(ukey).delete()
        except Exception as exc:
            log.error("Failed to delete user %s/%s from Firestore: %s", gkey, ukey, exc)
            raise
        log.warning("Deleted user record %s/%s (existed=%s)", gkey, ukey, existed)
        return existed

    # ── XP operations ────────────────────────────────────────────────────────

    async def add_xp(
        self, guild_id: int, user_id: int, amount: int
    ) -> tuple[int, int, bool]:
        """
        Add XP to a user. Returns (new_xp, new_level, leveled_up).
        Respects the cooldown from settings.
        """
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            now = time.time()

            # Check cooldown
            if now - user["last_xp_time"] < settings.XP_COOLDOWN_SECONDS:
                return user["xp"], user["level"], False

            old_level = user["level"]
            user["xp"] += amount
            user["messages"] += 1
            user["last_xp_time"] = now

            new_level = level_from_xp(user["xp"])
            user["level"] = new_level
            self._mark_dirty(guild_id, user_id)

            return user["xp"], new_level, new_level > old_level

    async def set_xp(self, guild_id: int, user_id: int, amount: int) -> None:
        """Directly set a user's XP (admin use)."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["xp"] = max(0, amount)
            user["level"] = level_from_xp(user["xp"])
            self._mark_dirty(guild_id, user_id)

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Add (or subtract) from a user's balance. Returns new balance.

        NOTE: use this only for credits (refunds, payouts) or deductions that are
        already known to be covered. For a spend that must not overdraw, use
        `try_debit` — `add_balance` clamps at 0, so a too-large deduction silently
        vanishes instead of failing, which a concurrent caller can exploit to spend
        coins they don't have (TOCTOU double-spend)."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["balance"] = max(0, user["balance"] + amount)
            self._mark_dirty(guild_id, user_id)
            return user["balance"]

    async def try_debit(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Atomically deduct `amount` only if the balance fully covers it.

        Returns True if the debit was applied, False on insufficient funds. The
        check and the deduction happen under one lock, so two concurrent requests
        can't both pass a balance check on the same funds and overdraw (the bug a
        separate get_user()+add_balance() pair has). A zero/negative amount is a
        no-op success. Never drives the balance below zero."""
        if amount <= 0:
            return True
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            if user["balance"] < amount:
                return False
            user["balance"] -= amount
            self._mark_dirty(guild_id, user_id)
            return True

    async def debit_up_to(self, guild_id: int, user_id: int, amount: int) -> int:
        """Atomically deduct up to `amount`, capped at the available balance.

        Returns the amount actually taken. For "take whatever they can pay" fines
        where a partial charge is intended; the read + deduction are atomic so the
        amount returned is exactly what left the account."""
        if amount <= 0:
            return 0
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            taken = min(amount, user["balance"])
            if taken > 0:
                user["balance"] -= taken
                self._mark_dirty(guild_id, user_id)
            return taken

    async def add_rescue(self, guild_id: int, user_id: int, amount: int = 1) -> int:
        """Increment a user's completed-rescue counter. Returns the new total."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["rescues"] = max(0, user.get("rescues", 0) + amount)
            self._mark_dirty(guild_id, user_id)
            return user["rescues"]

    async def add_unlocked_level(self, guild_id: int, user_id: int, level: int) -> bool:
        """Add a level to unlocked_levels if not already present. Returns True if newly added."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            # handle legacy data safely
            if "unlocked_levels" not in user:
                old_max = user.pop("max_unlocked_level", 0)
                user["unlocked_levels"] = [old_max] if old_max > 0 else []
                
            unlocked = set(user["unlocked_levels"])
            if level not in unlocked:
                unlocked.add(level)
                user["unlocked_levels"] = sorted(list(unlocked))
                self._mark_dirty(guild_id, user_id)
                return True
            return False

    async def remove_unlocked_level(self, guild_id: int, user_id: int, level: int) -> bool:
        """Remove a level from unlocked_levels. Use level=0 to clear all. Returns True if changed."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            if "unlocked_levels" not in user:
                old_max = user.pop("max_unlocked_level", 0)
                user["unlocked_levels"] = [old_max] if old_max > 0 else []
                
            unlocked = set(user["unlocked_levels"])
            if level == 0 and unlocked:
                user["unlocked_levels"] = []
                self._mark_dirty(guild_id, user_id)
                return True
            elif level in unlocked:
                unlocked.remove(level)
                user["unlocked_levels"] = sorted(list(unlocked))
                self._mark_dirty(guild_id, user_id)
                return True
            return False

    # ── Leaderboard ──────────────────────────────────────────────────────────

    def leaderboard(
        self, guild_id: int, key: str = "xp", limit: int | None = None
    ) -> list[tuple[str, UserData]]:
        """
        Return users sorted by `key` (descending).
        Each item is (user_id_str, user_data).
        """
        guild = self._guild(guild_id)
        limit = limit or settings.LEADERBOARD_PAGE_SIZE
        return sorted(
            guild.items(),
            key=lambda kv: kv[1].get(key, 0),
            reverse=True,
        )[:limit]


# Singleton – import this from anywhere
store = UserStore()
