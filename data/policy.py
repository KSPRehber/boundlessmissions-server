"""
data/policy.py – Privacy Policy / Terms of Service version registry (Firestore).

A single config document (`config/policy`) holds the current policy version. The
KSP client records which version the player accepted (consent.cfg); the server
advertises the current version on /version/check, and when the server's version
is higher the client re-prompts the opt-in gate and stops transmitting until the
player re-accepts. Bumping the version here is the only step needed to force a
fleet-wide re-consent — no mod rebuild.

Document shape:
    config/policy
    {
        "version":     2,                       # monotonically increasing int
        "summary":     "Clarified telemetry retention.",
        "privacy_url": "https://...",           # optional display overrides
        "terms_url":   "https://...",
        "updated_at":  "<iso8601>",
        "updated_by":  "<discord user>"
    }
"""

import logging
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# Baseline policy version. Shipped clients accept version 1, so the re-consent
# gate stays dormant until an admin publishes a higher version.
DEFAULT_VERSION = 1


def _doc():
    return _db.collection("config").document("policy")


def get_config() -> dict:
    """Current policy document (empty dict if nothing has been published yet)."""
    snap = _doc().get()
    return snap.to_dict() if snap.exists else {}


def get_version() -> int:
    """The policy version clients must have accepted. Falls back to the baseline
    if unset or malformed (so a missing/corrupt doc never forces a re-consent)."""
    try:
        return int(get_config().get("version") or DEFAULT_VERSION)
    except (TypeError, ValueError):
        return DEFAULT_VERSION


def set_version(version: int, updated_by: str, summary: str | None = None,
                privacy_url: str | None = None, terms_url: str | None = None) -> dict:
    """Publish a new policy version. Bumping this forces every client that
    accepted an older version to re-accept before it transmits again. Returns the
    stored document."""
    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}

    data["version"] = int(version)
    if summary is not None:
        data["summary"] = summary
    if privacy_url is not None:
        data["privacy_url"] = privacy_url
    if terms_url is not None:
        data["terms_url"] = terms_url
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = updated_by
    ref.set(data)
    return data
