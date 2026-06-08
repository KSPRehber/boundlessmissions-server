"""
api_auth.py – KSP ↔ Discord authentication layer.

Flow:
  1. User runs /g linkcode in Discord → 6-digit code stored in Firestore (10 min TTL)
  2. KSP client sends POST /api/v1/auth/link with the code
  3. Server validates, returns a signed session token (HMAC-SHA256)
  4. KSP stores the token locally, sends it with every request via Authorization header

No API keys, Firebase creds, or secrets ever touch the client.
"""

import hashlib
import hmac
import json
import logging
import random
import string
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# Token lifetime: 30 days
TOKEN_LIFETIME = 30 * 24 * 3600


def _link_codes_col():
    return _db.collection("ksp_link_codes")


def _sessions_col():
    return _db.collection("ksp_sessions")


# ── Link Codes ───────────────────────────────────────────────────────────────

def generate_link_code(guild_id: int, user_id: int, username: str) -> str:
    """Create a 6-digit code, store in Firestore with 10-min expiry. Returns the code."""
    # Invalidate any existing codes for this user
    for doc in _link_codes_col().where("user_id", "==", str(user_id)).stream():
        doc.reference.delete()

    code = "".join(random.choices(string.digits, k=6))
    _link_codes_col().document(code).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + 600,  # 10 minutes
    })
    log.info("Generated link code %s for user %s (%s)", code, user_id, username)
    return code


def validate_link_code(code: str) -> dict | None:
    """
    Check if a link code is valid and not expired.
    Returns {guild_id, user_id, username} or None.
    Deletes the code after successful validation (one-time use).
    """
    doc = _link_codes_col().document(code).get()
    if not doc.exists:
        return None

    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0):
        # Expired — clean up
        doc.reference.delete()
        return None

    # One-time use — delete
    doc.reference.delete()
    log.info("Validated link code %s for user %s", code, data["user_id"])
    return {
        "guild_id": data["guild_id"],
        "user_id": data["user_id"],
        "username": data["username"],
    }


# ── Session Tokens ───────────────────────────────────────────────────────────

def _sign_token(payload: dict, secret: str) -> str:
    """Create an HMAC-SHA256 signed token from a payload dict."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    # Token format: base64(payload).signature
    import base64
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"{encoded}.{sig}"


def _verify_token(token: str, secret: str) -> dict | None:
    """Verify an HMAC-signed token. Returns payload dict or None."""
    import base64
    parts = token.split(".")
    if len(parts) != 2:
        return None

    encoded, sig = parts
    try:
        raw = base64.urlsafe_b64decode(encoded).decode()
    except Exception:
        return None

    expected_sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    payload = json.loads(raw)

    # Check expiry
    if time.time() > payload.get("exp", 0):
        return None

    return payload


def create_session_token(guild_id: str, user_id: str, username: str, secret: str) -> str:
    """Create a signed session token and store session in Firestore."""
    now = time.time()
    payload = {
        "gid": guild_id,
        "uid": user_id,
        "usr": username,
        "iat": int(now),
        "exp": int(now + TOKEN_LIFETIME),
    }
    token = _sign_token(payload, secret)

    # Store session reference in Firestore
    _sessions_col().document(user_id).set({
        "guild_id": guild_id,
        "user_id": user_id,
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": payload["exp"],
        "active": True,
    })

    log.info("Created session token for user %s (guild %s)", user_id, guild_id)
    return token


def verify_session_token(token: str, secret: str) -> dict | None:
    """
    Verify a session token.
    Returns {guild_id, user_id, username} or None.
    """
    payload = _verify_token(token, secret)
    if payload is None:
        return None

    return {
        "guild_id": payload["gid"],
        "user_id": payload["uid"],
        "username": payload["usr"],
    }


def revoke_session(user_id: str) -> None:
    """Revoke a user's session."""
    doc = _sessions_col().document(user_id)
    if doc.get().exists:
        doc.update({"active": False})
        log.info("Revoked session for user %s", user_id)
