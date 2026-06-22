"""
cost_guard.py – Monthly spending cap for the paid Google services.

The bot depends on two metered Google products:

  • Gemini   – AI screenshot / mission / contract analysis.
  • Firebase – Firestore (every XP write, every contract) + Storage (craft files).

This module keeps a running ESTIMATE of how much each has cost so far this
month and reports whether each service is still within its budget (set in
settings.py / .env). Enforcement lives at the call sites:

  • Gemini  → soft: `guard.gemini_ok` is False, callers fall back to heuristics.
  • Firebase → hard: `guard.require_firebase()` raises `FirebaseBudgetExceeded`,
    wired into the Firestore/Storage wrappers in data/store.py.

State is persisted to a LOCAL JSON file, never to Firestore. That is deliberate:
the meter must keep working when Firebase itself is the service being cut off,
and metering must not itself cost Firestore operations. Budgets reset when the
UTC month rolls over.

The guard imports nothing from the bot except `settings`, so it is safe to
import from low-level modules like data/store.py without a circular import.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import settings

log = logging.getLogger(__name__)

# Live next to the other local-cache files (data/users.json).
_STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "cost_state.json")

# Don't rewrite the file on every single Firestore op (the XP path is hot);
# flush at most this often. State is also flushed whenever a budget flips.
_PERSIST_INTERVAL = 15.0


class FirebaseBudgetExceeded(RuntimeError):
    """Raised by Firestore/Storage wrappers once the Firebase budget is spent."""


def _month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


class _CostGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._month = _month_key()
        # Raw usage tallies for the current month.
        self._gemini_usd = 0.0
        self._reads = 0
        self._writes = 0
        self._deletes = 0
        self._dl_bytes = 0
        self._ul_bytes = 0
        # Cached "over budget" flags so the hot path avoids recompute + logs once.
        self._firebase_blocked = False
        self._gemini_blocked = False
        self._last_persist = 0.0
        self._dirty = False
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(_STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("month") != self._month:
            return  # stale month → start fresh
        self._gemini_usd = float(data.get("gemini_usd", 0.0))
        self._reads = int(data.get("reads", 0))
        self._writes = int(data.get("writes", 0))
        self._deletes = int(data.get("deletes", 0))
        self._dl_bytes = int(data.get("dl_bytes", 0))
        self._ul_bytes = int(data.get("ul_bytes", 0))

    def _persist_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_persist) < _PERSIST_INTERVAL:
            return
        self._last_persist = now
        self._dirty = False
        payload = {
            "month": self._month,
            "gemini_usd": round(self._gemini_usd, 6),
            "reads": self._reads,
            "writes": self._writes,
            "deletes": self._deletes,
            "dl_bytes": self._dl_bytes,
            "ul_bytes": self._ul_bytes,
            "firebase_usd": round(self._firebase_usd(), 6),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            tmp = _STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, _STATE_PATH)
        except OSError as exc:
            log.warning("cost_guard: could not persist state: %s", exc)

    def flush(self) -> None:
        """Force-write the current tallies (call on shutdown)."""
        with self._lock:
            self._persist_locked(force=True)

    # ── derived cost ──────────────────────────────────────────────────────────
    def _firebase_usd(self) -> float:
        return (
            self._reads / 100_000 * settings.FIRESTORE_READ_USD_PER_100K
            + self._writes / 100_000 * settings.FIRESTORE_WRITE_USD_PER_100K
            + self._deletes / 100_000 * settings.FIRESTORE_DELETE_USD_PER_100K
            + self._dl_bytes / 1_073_741_824 * settings.STORAGE_DOWNLOAD_USD_PER_GB
            + self._ul_bytes / 1_073_741_824 * settings.STORAGE_UPLOAD_USD_PER_GB
        )

    def _rollover_locked(self) -> None:
        """Reset tallies if the UTC month changed since they were recorded."""
        current = _month_key()
        if current == self._month:
            return
        log.info("cost_guard: new month %s — resetting spend tallies", current)
        self._month = current
        self._gemini_usd = 0.0
        self._reads = self._writes = self._deletes = 0
        self._dl_bytes = self._ul_bytes = 0
        self._firebase_blocked = self._gemini_blocked = False
        self._persist_locked(force=True)

    # ── status (read by call sites) ─────────────────────────────────────────
    @property
    def gemini_ok(self) -> bool:
        if not settings.COST_GUARD_ENABLED:
            return True
        budget = settings.GEMINI_MONTHLY_BUDGET_USD
        if budget <= 0:
            return True  # unlimited
        with self._lock:
            self._rollover_locked()
            over = self._gemini_usd >= budget
            if over and not self._gemini_blocked:
                self._gemini_blocked = True
                log.warning(
                    "cost_guard: Gemini budget hit ($%.2f / $%.2f) — AI degraded "
                    "to fallbacks until %s rolls over.",
                    self._gemini_usd, budget, self._month,
                )
            elif not over:
                self._gemini_blocked = False
            return not over

    @property
    def firebase_ok(self) -> bool:
        if not settings.COST_GUARD_ENABLED:
            return True
        budget = settings.FIREBASE_MONTHLY_BUDGET_USD
        if budget <= 0:
            return True  # unlimited
        with self._lock:
            self._rollover_locked()
            spent = self._firebase_usd()
            over = spent >= budget
            if over and not self._firebase_blocked:
                self._firebase_blocked = True
                log.error(
                    "cost_guard: Firebase budget hit ($%.2f / $%.2f) — Firestore "
                    "and Storage are HARD-STOPPED until %s rolls over.",
                    spent, budget, self._month,
                )
            elif not over:
                self._firebase_blocked = False
            return not over

    def require_firebase(self) -> None:
        """Raise if the Firebase budget is spent (hard stop). No-op otherwise."""
        if not self.firebase_ok:
            raise FirebaseBudgetExceeded(
                "Firebase monthly budget exceeded; Firestore/Storage access is "
                "blocked until the budget resets."
            )

    # ── recording usage ────────────────────────────────────────────────────
    def record_gemini(self, usage) -> None:
        """Add the cost of one Gemini call from its response.usage_metadata."""
        if usage is None:
            return
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        cost = (
            in_tok / 1_000_000 * settings.GEMINI_INPUT_USD_PER_1M
            + out_tok / 1_000_000 * settings.GEMINI_OUTPUT_USD_PER_1M
        )
        with self._lock:
            self._rollover_locked()
            self._gemini_usd += cost
            self._persist_locked(force=True)  # rare event — persist immediately

    def note_firestore(self, reads: int = 0, writes: int = 0, deletes: int = 0) -> None:
        if not (reads or writes or deletes):
            return
        with self._lock:
            self._rollover_locked()
            self._reads += reads
            self._writes += writes
            self._deletes += deletes
            self._persist_locked()

    def note_storage(self, download: int = 0, upload: int = 0) -> None:
        if not (download or upload):
            return
        with self._lock:
            self._rollover_locked()
            self._dl_bytes += download
            self._ul_bytes += upload
            self._persist_locked()

    def snapshot(self) -> dict:
        """Current spend with a per-component USD breakdown, for an admin command."""
        with self._lock:
            self._rollover_locked()
            g_budget = settings.GEMINI_MONTHLY_BUDGET_USD
            f_budget = settings.FIREBASE_MONTHLY_BUDGET_USD
            reads_usd = self._reads / 100_000 * settings.FIRESTORE_READ_USD_PER_100K
            writes_usd = self._writes / 100_000 * settings.FIRESTORE_WRITE_USD_PER_100K
            deletes_usd = self._deletes / 100_000 * settings.FIRESTORE_DELETE_USD_PER_100K
            dl_usd = self._dl_bytes / 1_073_741_824 * settings.STORAGE_DOWNLOAD_USD_PER_GB
            ul_usd = self._ul_bytes / 1_073_741_824 * settings.STORAGE_UPLOAD_USD_PER_GB
            firebase_usd = self._firebase_usd()
            return {
                "enabled": settings.COST_GUARD_ENABLED,
                "month": self._month,
                "gemini": {
                    "usd": self._gemini_usd,
                    "budget": g_budget,
                    "ok": self._gemini_usd < g_budget if g_budget > 0 else True,
                    "unlimited": g_budget <= 0,
                },
                "firebase": {
                    "usd": firebase_usd,
                    "budget": f_budget,
                    "ok": firebase_usd < f_budget if f_budget > 0 else True,
                    "unlimited": f_budget <= 0,
                    "lines": [
                        ("Firestore reads", self._reads, reads_usd),
                        ("Firestore writes", self._writes, writes_usd),
                        ("Firestore deletes", self._deletes, deletes_usd),
                        ("Storage download", self._dl_bytes, dl_usd),
                        ("Storage upload", self._ul_bytes, ul_usd),
                    ],
                },
            }


guard = _CostGuard()
