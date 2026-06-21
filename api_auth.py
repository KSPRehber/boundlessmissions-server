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
import secrets
import string
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# Token lifetime: 30 days
TOKEN_LIFETIME = 30 * 24 * 3600

# Link code / 2FA challenge lifetime: 3 minutes
LINK_CODE_LIFETIME = 180
TWOFA_LIFETIME = 180
TWOFA_MAX_ATTEMPTS = 5


def _link_codes_col():
    return _db.collection("ksp_link_codes")


def _sessions_col():
    return _db.collection("ksp_sessions")


def _twofa_col():
    return _db.collection("ksp_2fa_challenges")


def _digit_code(n: int = 6) -> str:
    """A cryptographically secure n-digit numeric code."""
    return "".join(secrets.choice(string.digits) for _ in range(n))


# ── Token Versioning (for "log out of all devices") ──────────────────────────
#
# Session tokens are stateless HMAC tokens, so there's nothing to "delete" to
# revoke them. Instead each user has a monotonically increasing token_version
# stored in their session doc; every token embeds the version it was minted at.
# verify_session_token rejects any token whose version is older than the user's
# current one, so bumping the version (logout_all_devices) instantly invalidates
# every token ever issued — across all devices — without touching the secret.
#
# The version is read on every request, so it's cached in-memory for a short TTL
# to keep verification cheap. logout_all_devices updates the cache in-process, so
# revocation takes effect immediately within the running bot (and within the TTL
# for the read-through path).

_TOKEN_VERSION_TTL = 30  # seconds
_token_versions: dict[str, tuple[int, float]] = {}  # user_id -> (version, fetched_at)


def _get_token_version(user_id: str) -> int:
    """Current token-revocation version for a user (0 if never revoked)."""
    cached = _token_versions.get(user_id)
    now = time.time()
    if cached is not None and now - cached[1] < _TOKEN_VERSION_TTL:
        return cached[0]

    version = 0
    try:
        snap = _sessions_col().document(user_id).get()
        if snap.exists:
            version = int(snap.to_dict().get("token_version", 0) or 0)
    except Exception as exc:
        log.warning("Could not read token version for %s: %s", user_id, exc)
        # On a read failure, prefer the last known value over silently
        # accepting tokens we can't validate the version of.
        if cached is not None:
            return cached[0]

    _token_versions[user_id] = (version, now)
    return version


# ── Link Codes ───────────────────────────────────────────────────────────────

def generate_link_code(guild_id: int, user_id: int, username: str) -> str:
    """Create a 6-digit code, store in Firestore with 10-min expiry. Returns the code."""
    # Invalidate any existing codes for this user
    for doc in _link_codes_col().where("user_id", "==", str(user_id)).stream():
        doc.reference.delete()

    code = _digit_code(6)
    _link_codes_col().document(code).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + LINK_CODE_LIFETIME,  # 3 minutes
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


# ── Discord DM 2FA ───────────────────────────────────────────────────────────
#
# Second factor for linking: a valid link code earns a one-time numeric code
# that's DM'd to the Discord user, who must enter it back in KSP to finish. The
# OTP is stored only as a salted hash, is single-use, attempt-capped, and expires
# in 3 minutes. Gated by cfg.KSP_2FA_ENABLED so it can be turned off for testing.

def _hash_otp(challenge_id: str, otp: str) -> str:
    return hashlib.sha256(f"{challenge_id}:{otp}".encode()).hexdigest()


def create_2fa_challenge(guild_id: str, user_id: str, username: str) -> tuple[str, str]:
    """Create a pending 2FA challenge. Returns (challenge_id, otp); the caller
    DMs the otp to the user and never stores it in the clear."""
    challenge_id = secrets.token_urlsafe(18)
    otp = _digit_code(6)
    _twofa_col().document(challenge_id).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "otp_hash": _hash_otp(challenge_id, otp),
        "attempts": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + TWOFA_LIFETIME,
    })
    log.info("Created 2FA challenge for user %s", user_id)
    return challenge_id, otp


def verify_2fa_challenge(challenge_id: str, otp: str) -> dict | None:
    """Validate a 2FA OTP. Returns {guild_id, user_id, username} on success
    (consuming the challenge), or None on wrong/expired/exhausted code."""
    doc = _twofa_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return None

    data = snap.to_dict()
    if time.time() > data.get("expires_at", 0):
        doc.delete()
        return None
    if data.get("attempts", 0) >= TWOFA_MAX_ATTEMPTS:
        doc.delete()  # too many tries — burn it
        return None

    if not hmac.compare_digest(data.get("otp_hash", ""), _hash_otp(challenge_id, otp)):
        # Wrong code — count the attempt; delete once the cap is hit.
        attempts = data.get("attempts", 0) + 1
        if attempts >= TWOFA_MAX_ATTEMPTS:
            doc.delete()
        else:
            doc.update({"attempts": attempts})
        return None

    doc.delete()  # one-time use
    log.info("Validated 2FA challenge for user %s", data["user_id"])
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
    # Mint the token at the user's current version. A fresh login does NOT bump
    # the version — only logout_all_devices does — so logging in on a new device
    # never invalidates the user's other devices.
    version = _get_token_version(user_id)
    payload = {
        "gid": guild_id,
        "uid": user_id,
        "usr": username,
        "iat": int(now),
        "exp": int(now + TOKEN_LIFETIME),
        "tv": version,
    }
    token = _sign_token(payload, secret)

    # Store session reference in Firestore. merge=True preserves token_version if
    # a previous logout_all_devices already set it for this user.
    _sessions_col().document(user_id).set({
        "guild_id": guild_id,
        "user_id": user_id,
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": payload["exp"],
        "token_version": version,
        "active": True,
    }, merge=True)

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

    # Reject tokens minted before the user's last "log out of all devices".
    # Legacy tokens predating versioning carry no "tv" (treated as 0) and stay
    # valid until the first logout_all_devices bumps the version above 0.
    if int(payload.get("tv", 0)) < _get_token_version(payload["uid"]):
        return None

    return {
        "guild_id": payload["gid"],
        "user_id": payload["uid"],
        "username": payload["usr"],
    }


def logout_all_devices(user_id: str) -> int:
    """Log the user out of every device by bumping their token version.

    All session tokens ever issued to this user — including the one that made
    this request — fail verification on their next API call and the KSP client
    drops to its unlinked state (it clears the token on any 401). Returns the new
    token version. This is the user's own privacy control, not an admin action.
    """
    doc = _sessions_col().document(user_id)
    snap = doc.get()
    current = 0
    if snap.exists:
        current = int(snap.to_dict().get("token_version", 0) or 0)
    new_version = current + 1

    doc.set({
        "token_version": new_version,
        "active": False,
        "logged_out_all_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    # Update the in-process cache so revocation is effective immediately.
    _token_versions[user_id] = (new_version, time.time())

    log.info("User %s logged out of all devices (token version → %d)", user_id, new_version)
    return new_version
