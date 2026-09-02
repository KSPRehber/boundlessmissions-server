"""
data/mod_version.py – KSP mod version registry (Firestore).

Stores the published mod versions and which one is current under a single config
document (`config/mod_version`). The KSP client reports its DLL's SHA256; the
server matches it against the latest published hash to decide whether the client
must update.

Document shape:
    config/mod_version
    {
        "latest_version": "1.2.0",
        "latest_hash":    "<sha256 hex>",
        "download_url":   "https://.../download",
        "versions": {                       # history, newest publish wins per label
            "1.2.0": {"hash": "<sha256>", "download_url": "https://..."},
            "1.1.0": {"hash": "<sha256>", "download_url": "https://..."}
        },
        "updated_at": "<iso8601>",
        "updated_by": "<discord user>"
    }
"""

import logging
import threading
import time
from datetime import datetime, timezone

from data.store import _db, _storage_bucket

log = logging.getLogger(__name__)

# `config/mod_version` is read on the anonymous, unauthenticated `/version/check`
# — once per client start, and once per retry of a client that is being told to
# update — plus by every path that resolves the latest hash. One document that
# changes when the owner publishes a build, read at the rate of the whole player
# base: that is a metered Firestore read per anonymous request, which is a free
# amplifier pointed at `cost_guard`.
#
# So it is memoised here, at the source, rather than at any one caller — `check()`
# calls `get_config()` internally, so a cache in front of the route would leave
# the default gate-enabled path uncached, which is the finding.
#
# The TTL is short on purpose. It is a backstop for an edit made outside this
# process (the Firestore console, another instance); the console's own publish
# calls `invalidate()`, so a change made through the product takes effect at once
# rather than up to a minute later. A failed read is NOT cached — an outage must
# not be remembered as "nothing is published", which is the answer that ungates
# every client.
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "doc": None}

# Pristine DLL bytes per hash, cached in-process so attestation doesn't hit
# Storage on every challenge. Keyed by lowercase sha256 hex.
_dll_cache: dict[str, bytes] = {}


def _doc():
    return _db.collection("config").document("mod_version")


def _dll_path(sha256: str) -> str:
    return f"mod_dll/{sha256}.dll"


def get_config(*, fresh: bool = False) -> dict:
    """Current version-registry document (empty dict if nothing published yet).

    Answered from a 60-second in-process cache; `invalidate()` clears it. Pass
    `fresh=True` to force a read — the publish path does, since it edits the
    document it just read.
    """
    if not fresh:
        with _cache_lock:
            doc, at = _cache["doc"], float(_cache["at"])
        if doc is not None and (time.time() - at) < _CACHE_TTL:
            return dict(doc)
    snap = _doc().get()
    data = snap.to_dict() if snap.exists else {}
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return dict(data)


def invalidate() -> None:
    """Drop the memoised registry document, so the next read hits Firestore.
    Called by the console after publishing a version."""
    with _cache_lock:
        _cache["doc"] = None
        _cache["at"] = 0.0


def publish_version(version: str, sha256: str, download_url: str,
                    set_latest: bool, updated_by: str,
                    dll_bytes: bytes | None = None) -> dict:
    """Register a version's DLL hash + download URL, optionally marking it latest.

    The first version published is always made latest (so the gate has a target),
    even if set_latest is False. If `dll_bytes` is provided, the pristine DLL is
    stored so the server can answer challenge-response attestations for it (the
    bytes are the only way to verify a nonce-salted hash the client can't precompute).
    Returns the stored document.
    """
    version = version.strip()
    sha256 = sha256.strip().lower()
    download_url = download_url.strip()

    # https only, checked HERE rather than at a call site. The web console enforced this
    # and explained why ("a compromised owner session shouldn't be able to redirect the
    # whole player base to a hostile download"); the Discord `/publishversion` command
    # did not, so the same reasoning protected one of the two publish paths. The value is
    # stored in config/mod_version and served to every client by /version/check, and
    # although the mod's own SafeDownloadUrl drops anything that is not absolute https,
    # nothing guarantees a future client or a third-party tool reading that field will.
    # A rule that belongs to the data belongs where the data is written.
    if download_url and not download_url.lower().startswith("https://"):
        raise ValueError("download_url must be an https:// URL.")

    # Persist the pristine bytes for attestation (best-effort; never block publish).
    has_dll = False
    if dll_bytes is not None and _storage_bucket is not None:
        try:
            blob = _storage_bucket.blob(_dll_path(sha256))
            blob.upload_from_string(dll_bytes, content_type="application/octet-stream")
            _dll_cache[sha256] = dll_bytes
            has_dll = True
            log.info("Stored pristine DLL for attestation (%s, %d bytes)", sha256[:12], len(dll_bytes))
        except Exception as exc:
            log.warning("Could not store pristine DLL for %s: %s", version, exc)

    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}

    versions = data.get("versions") or {}
    versions[version] = {"hash": sha256, "download_url": download_url, "has_dll": has_dll}
    data["versions"] = versions

    if set_latest or not data.get("latest_hash"):
        data["latest_version"] = version
        data["latest_hash"] = sha256
        data["download_url"] = download_url
        data["has_dll"] = has_dll

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = updated_by
    ref.set(data)
    # The writer refreshes the cache rather than only clearing it: a publish is
    # immediately followed by the version poke, and a cache left empty would send
    # every client that reacts to it straight to Firestore at once.
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return data


def get_latest_dll_bytes() -> tuple[str, bytes] | None:
    """Return (sha256, pristine_bytes) for the published-latest DLL, or None if no
    DLL was stored (attestation unavailable → callers fail open). Cached in-process."""
    cfg_doc = get_config()
    h = (cfg_doc.get("latest_hash") or "").lower()
    if not h or not cfg_doc.get("has_dll"):
        return None
    cached = _dll_cache.get(h)
    if cached is not None:
        return h, cached
    if _storage_bucket is None:
        return None
    try:
        blob = _storage_bucket.blob(_dll_path(h))
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        _dll_cache[h] = data
        return h, data
    except Exception as exc:
        log.warning("Could not load pristine DLL for attestation (%s): %s", h[:12], exc)
        return None


def check(client_hash: str, client_version: str) -> dict:
    """Compare a client's reported DLL hash against the published latest.

    Fails open: if nothing is published yet (no latest hash), the client is never
    blocked. Returns a dict matching VersionCheckResponse.
    """
    cfg_doc = get_config()
    latest_hash = (cfg_doc.get("latest_hash") or "").lower()
    latest_version = cfg_doc.get("latest_version")
    download_url = cfg_doc.get("download_url")

    if not latest_hash:
        # Nothing published — don't gate anyone (no hash to advertise yet).
        return {"enabled": True, "up_to_date": True, "latest_hash": None,
                "your_version": client_version or None}

    up_to_date = bool(client_hash) and client_hash.strip().lower() == latest_hash
    return {
        "enabled": True,
        "up_to_date": up_to_date,
        "latest_version": latest_version,
        "latest_hash": latest_hash,
        "download_url": download_url,
        "your_version": client_version or None,
        "message": None if up_to_date else f"A new version ({latest_version}) is available.",
    }
