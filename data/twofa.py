"""
data/twofa.py – TOTP second factor and recovery codes.

Why this exists at all: the second factor for a Discord-linked account has always
been "press the button in your DM", which is a genuine check — it needs an
interaction inside the user's own Discord, which a stolen code cannot produce. But
it only works for people who *have* a Discord, and it hard-fails for anyone with
DMs closed (the 502 in `web_auth_link`). A website account has neither a DM nor a
fallback, so it needs a factor of its own.

**The protocol is hand-rolled; only the QR is not.** RFC 6238 is HMAC-SHA1 over a
counter and a truncation, all of it in the standard library — the same call this
project makes in `gcp_metrics` / `gcp_billing`, which speak REST directly rather
than pull in client libraries. A QR encoder is a different proposition: Reed-Solomon,
version selection and mask evaluation are far past the size where hand-rolling is
the cheaper option, and a subtle bug there produces a code some scanners read and
others do not. So `segno` (pure Python, no dependencies of its own) draws it, and
everything security-relevant stays here.

Three details are load-bearing:

  • **Secrets live in their own collection**, not on the account document. The
    account doc is read to render a profile; a TOTP secret is a credential, and
    credentials should not ride along in something fetched to draw a name.
  • **A used code cannot be used again.** `last_counter` is stored and the check
    refuses anything at or below it, so a code shoulder-surfed inside its 30-second
    window is already spent by the time it is retyped.
  • **Recovery codes are stored hashed and single-use** — the only way back in when
    the phone is gone, and the reason enabling TOTP is not a way to lose an account.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# RFC 6238 defaults, which is what every authenticator app assumes.
DIGITS = 6
PERIOD = 30

# One step either side of now. Enough for a phone clock that drifts a little or a
# user who types slowly; more than that starts widening the window a stolen code
# is usable in.
DEFAULT_WINDOW = 1

RECOVERY_CODE_COUNT = 10
LOGIN_CHALLENGE_LIFETIME = 300   # 5 minutes to type a 6-digit code


def _col():
    return _db.collection("account_2fa")


def _challenges():
    return _db.collection("twofa_login")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── TOTP itself ──────────────────────────────────────────────────────────────

def generate_secret(length: int = 20) -> str:
    """A fresh base32 secret. 20 bytes = 160 bits, the RFC 4226 recommendation."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    """One HOTP value — RFC 4226. TOTP is this with counter = time // period."""
    # Authenticator apps strip padding from the secret they show; put it back or
    # b32decode refuses a string whose length is not a multiple of 8.
    padded = secret_b32.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    key = base64.b32decode(padded, casefold=True)

    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** DIGITS)).zfill(DIGITS)


def counter_now(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // PERIOD)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    """The code an authenticator app is showing right now. Mostly for tests."""
    return _hotp(secret_b32, counter_now(at))


def match_counter(secret_b32: str, code: str, *, window: int = DEFAULT_WINDOW,
                  at: float | None = None) -> int | None:
    """The counter a code matches, or None.

    Returns the counter rather than a bool because the caller has to record it:
    without that, a code stays valid for its whole window and can be replayed.
    Compared with `compare_digest` so the check does not leak timing.
    """
    entered = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(entered) != DIGITS:
        return None
    base = counter_now(at)
    for drift in range(-window, window + 1):
        counter = base + drift
        if counter < 0:
            continue
        if hmac.compare_digest(_hotp(secret_b32, counter), entered):
            return counter
    return None


def provisioning_uri(secret_b32: str, account_name: str,
                     issuer: str = "Boundless Missions") -> str:
    """The `otpauth://` URI an authenticator app imports.

    Handed to the user three ways — as a QR (see `provisioning_qr_svg`), as this
    link, and as the raw secret for manual entry. The link is what opens the app
    directly on a phone; the secret is the fallback when a camera is not an option.
    """
    from urllib.parse import quote
    label = quote(f"{issuer}:{account_name}", safe="")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer, safe='')}&digits={DIGITS}&period={PERIOD}")


def provisioning_qr_svg(uri: str, scale: int = 5) -> str:
    """The provisioning URI as an inline SVG QR code, or "" if it can't be made.

    Rendered server-side rather than in the browser so every surface gets the same
    picture from one implementation — and so the page needs no QR library and no
    CSP widening: the SVG carries no script and no external reference, so it can be
    inlined directly.

    Error correction M (not L) because this is scanned off a screen at whatever
    size the layout gives it, often at an angle; the extra redundancy costs a
    slightly denser code and buys a much better first-try scan rate.

    Returns "" rather than raising: a missing QR leaves manual entry, which every
    authenticator supports, and is not a reason to fail the whole enrolment.
    """
    try:
        import segno
        return segno.make(uri, error="m").svg_inline(scale=scale, border=2)
    except Exception as exc:
        log.warning("Could not render a QR for the provisioning URI: %s", exc)
        return ""


# ── Recovery codes ───────────────────────────────────────────────────────────

def _hash_code(code: str) -> str:
    """Recovery codes are stored hashed, like any other credential — a leaked
    database must not hand over a way in. Plain SHA-256 is right here (unlike for
    a password): these are 40+ bits of our own randomness, so there is no
    dictionary to attack and nothing for a slow KDF to buy."""
    return hashlib.sha256(str(code or "").strip().upper().encode()).hexdigest()


def generate_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Human-transcribable one-time codes, shown once and never again."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I/O/0/1
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


# ── Enrolment state ──────────────────────────────────────────────────────────

def status(account_id) -> dict:
    """`{enabled, pending, recovery_remaining}` — never the secret itself."""
    try:
        snap = _col().document(str(account_id)).get()
    except Exception as exc:
        log.warning("Could not read 2FA status for %s: %s", account_id, exc)
        return {"enabled": False, "pending": False, "recovery_remaining": 0}
    if not snap.exists:
        return {"enabled": False, "pending": False, "recovery_remaining": 0}
    d = snap.to_dict() or {}
    return {
        "enabled": bool(d.get("enabled")),
        "pending": bool(d.get("secret")) and not d.get("enabled"),
        "recovery_remaining": len(d.get("recovery_hashes") or []),
    }


def is_enabled(account_id) -> bool:
    """Whether this account requires a code at sign-in.

    Fails **closed** on an unreadable record, unlike most reads in this codebase:
    everywhere else a failed read costs a feature, but here it would cost the
    second factor itself — and silently letting someone past their own 2FA because
    Firestore hiccuped is the one outcome this whole module exists to prevent.
    Returning True on an outage means a sign-in that cannot complete, which is a
    much better failure than one that completes when it should not.
    """
    try:
        snap = _col().document(str(account_id)).get()
    except Exception as exc:
        log.warning("2FA check for %s failed; refusing rather than skipping: %s",
                    account_id, exc)
        return True
    return bool(snap.exists and (snap.to_dict() or {}).get("enabled"))


def begin_enroll(account_id, account_name: str) -> dict | None:
    """Mint a secret and hold it unconfirmed. Returns {secret, uri}.

    Nothing is enforced until `confirm_enroll` sees a working code — enabling on
    the strength of a secret nobody has proved they can read would lock people out
    of their own accounts with a QR they never scanned.
    """
    secret = generate_secret()
    try:
        _col().document(str(account_id)).set({
            "account_id": str(account_id),
            "secret": secret,
            "enabled": False,
            "created_at": _now(),
            "recovery_hashes": [],
            "last_counter": 0,
        })
    except Exception as exc:
        log.warning("Could not begin 2FA enrolment for %s: %s", account_id, exc)
        return None
    return {"secret": secret, "uri": provisioning_uri(secret, account_name)}


def confirm_enroll(account_id, code: str) -> tuple[bool, str, list[str]]:
    """Turn 2FA on, once a real code proves the app is set up.

    Returns (ok, message, recovery_codes). The codes are returned exactly once —
    only their hashes are kept — so the caller must show them there and then.
    """
    ref = _col().document(str(account_id))
    try:
        snap = ref.get()
    except Exception as exc:
        log.warning("Could not read 2FA enrolment for %s: %s", account_id, exc)
        return False, "Couldn't check that just now. Try again.", []
    if not snap.exists:
        return False, "Start setting up two-factor authentication first.", []

    d = snap.to_dict() or {}
    if d.get("enabled"):
        return False, "Two-factor authentication is already on.", []
    secret = str(d.get("secret") or "")
    if not secret:
        return False, "Start setting up two-factor authentication first.", []

    counter = match_counter(secret, code)
    if counter is None:
        return False, "That code isn't right. Check your authenticator app and try again.", []

    codes = generate_recovery_codes()
    try:
        ref.set({
            "enabled": True,
            "enabled_at": _now(),
            "last_counter": counter,
            "recovery_hashes": [_hash_code(c) for c in codes],
        }, merge=True)
    except Exception as exc:
        log.warning("Could not enable 2FA for %s: %s", account_id, exc)
        return False, "Couldn't turn it on just now. Try again.", []

    log.info("2FA enabled for account %s", account_id)
    return True, "Two-factor authentication is on.", codes


def verify(account_id, code: str) -> tuple[bool, str]:
    """Check a code at sign-in. Accepts a TOTP code or a recovery code.

    A matched TOTP counter is recorded so the same code cannot be presented twice,
    and a matched recovery code is removed — both are one-shot by design.
    """
    ref = _col().document(str(account_id))
    try:
        snap = ref.get()
    except Exception as exc:
        log.warning("Could not read 2FA for %s: %s", account_id, exc)
        return False, "Couldn't check that just now. Try again."
    if not snap.exists or not (snap.to_dict() or {}).get("enabled"):
        return False, "Two-factor authentication isn't set up on this account."

    d = snap.to_dict() or {}
    secret = str(d.get("secret") or "")
    last = int(d.get("last_counter", 0) or 0)

    counter = match_counter(secret, code) if secret else None
    if counter is not None:
        if counter <= last:
            # Correct, but already spent. Refusing is the point: otherwise a code
            # read over someone's shoulder stays good for the rest of its window.
            return False, "That code has already been used. Wait for the next one."
        try:
            ref.set({"last_counter": counter, "last_used_at": _now()}, merge=True)
        except Exception as exc:
            log.warning("Could not record 2FA counter for %s: %s", account_id, exc)
        return True, "ok"

    # Not a TOTP code — try the recovery list.
    hashed = _hash_code(code)
    remaining = list(d.get("recovery_hashes") or [])
    if hashed in remaining:
        remaining.remove(hashed)
        try:
            ref.set({"recovery_hashes": remaining,
                     "last_used_at": _now()}, merge=True)
        except Exception as exc:
            log.warning("Could not spend recovery code for %s: %s", account_id, exc)
            return False, "Couldn't check that just now. Try again."
        log.warning("Account %s signed in with a recovery code (%d left)",
                    account_id, len(remaining))
        return True, f"Recovery code accepted. {len(remaining)} left."

    return False, "That code isn't right."


def disable(account_id, code: str) -> tuple[bool, str]:
    """Turn 2FA off. Requires a working code — otherwise anyone who borrowed an
    already-signed-in browser could strip the very protection it is there to be."""
    ok, message = verify(account_id, code)
    if not ok:
        return False, message
    try:
        _col().document(str(account_id)).delete()
    except Exception as exc:
        log.warning("Could not disable 2FA for %s: %s", account_id, exc)
        return False, "Couldn't turn it off just now. Try again."
    log.warning("2FA disabled for account %s", account_id)
    return True, "Two-factor authentication is off."


def regenerate_recovery_codes(account_id, code: str) -> tuple[bool, str, list[str]]:
    """A fresh set, replacing the old ones. Gated on a working code for the same
    reason `disable` is."""
    ok, message = verify(account_id, code)
    if not ok:
        return False, message, []
    codes = generate_recovery_codes()
    try:
        _col().document(str(account_id)).set(
            {"recovery_hashes": [_hash_code(c) for c in codes]}, merge=True)
    except Exception as exc:
        log.warning("Could not regenerate recovery codes for %s: %s", account_id, exc)
        return False, "Couldn't do that just now. Try again.", []
    return True, "New recovery codes generated. The old ones no longer work.", codes


def purge(account_id) -> None:
    """Drop the second factor entirely, for account deletion. No code required —
    the caller is a moderator erasing the account, not the user."""
    try:
        _col().document(str(account_id)).delete()
    except Exception as exc:
        log.warning("Could not purge 2FA for %s: %s", account_id, exc)


# ── The sign-in challenge ────────────────────────────────────────────────────
#
# Sign-in proves the Firebase identity, then stops and asks for a code. The
# half-finished state has to live somewhere between the two requests, and it must
# NOT be a session token — a token that works without the second factor is the
# thing 2FA is supposed to prevent.

def create_login_challenge(account_id, payload: dict | None = None) -> str | None:
    """`payload` rides along for a challenge that is finishing something other than
    a website sign-in — the KSP link flow stores the validated link code's result
    here, because that code is one-time and is already spent by this point."""
    challenge_id = secrets.token_urlsafe(18)
    try:
        _challenges().document(challenge_id).set({
            "account_id": str(account_id),
            "payload": payload or {},
            "created_at": _now(),
            "expires_at": time.time() + LOGIN_CHALLENGE_LIFETIME,
            "attempts": 0,
        })
    except Exception as exc:
        log.warning("Could not create 2FA login challenge for %s: %s", account_id, exc)
        return None
    return challenge_id


def resolve_login_challenge(challenge_id: str, code: str) -> tuple[str | None, str, dict]:
    """Spend a challenge with a valid code. Returns (account_id, message).

    Attempts are counted on the challenge itself and capped: a 6-digit code is
    only a million guesses, so without this the challenge window is a brute-force
    opportunity rather than a checkpoint.
    """
    ref = _challenges().document(str(challenge_id))
    try:
        snap = ref.get()
    except Exception as exc:
        log.warning("Could not read 2FA challenge: %s", exc)
        return None, "Couldn't check that just now. Try again.", {}
    if not snap.exists:
        return None, "That sign-in expired. Start again.", {}

    d = snap.to_dict() or {}
    if time.time() > d.get("expires_at", 0):
        try:
            ref.delete()
        except Exception:
            pass
        return None, "That sign-in expired. Start again.", {}

    attempts = int(d.get("attempts", 0) or 0)
    if attempts >= 5:
        try:
            ref.delete()
        except Exception:
            pass
        return None, "Too many wrong codes. Start again.", {}

    account_id = str(d.get("account_id") or "")
    ok, message = verify(account_id, code)
    if not ok:
        try:
            ref.set({"attempts": attempts + 1}, merge=True)
        except Exception:
            pass
        return None, message, {}

    try:
        ref.delete()
    except Exception as exc:
        log.warning("Could not consume 2FA challenge: %s", exc)
    return account_id, message, dict(d.get("payload") or {})
