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

from datetime import datetime, timezone

from data.store import _db


def _doc():
    return _db.collection("config").document("mod_version")


def get_config() -> dict:
    """Current version-registry document (empty dict if nothing published yet)."""
    snap = _doc().get()
    return snap.to_dict() if snap.exists else {}


def publish_version(version: str, sha256: str, download_url: str,
                    set_latest: bool, updated_by: str) -> dict:
    """Register a version's DLL hash + download URL, optionally marking it latest.

    The first version published is always made latest (so the gate has a target),
    even if set_latest is False. Returns the stored document.
    """
    version = version.strip()
    sha256 = sha256.strip().lower()
    download_url = download_url.strip()

    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}

    versions = data.get("versions") or {}
    versions[version] = {"hash": sha256, "download_url": download_url}
    data["versions"] = versions

    if set_latest or not data.get("latest_hash"):
        data["latest_version"] = version
        data["latest_hash"] = sha256
        data["download_url"] = download_url

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = updated_by
    ref.set(data)
    return data


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
        # Nothing published — don't gate anyone.
        return {"enabled": True, "up_to_date": True, "your_version": client_version or None}

    up_to_date = bool(client_hash) and client_hash.strip().lower() == latest_hash
    return {
        "enabled": True,
        "up_to_date": up_to_date,
        "latest_version": latest_version,
        "download_url": download_url,
        "your_version": client_version or None,
        "message": None if up_to_date else f"A new version ({latest_version}) is available.",
    }
