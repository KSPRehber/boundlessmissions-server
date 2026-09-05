"""
data/mp_keys.py – the multiplayer token signing key, and the tokens it mints.

This is the **account service's half** of the multiplayer trust model. Its
counterpart is `common/tokens.py` in the Multiplayer-Server repo, and the two are
deliberately *not* shared code: they are separate repositories, so they are
duplicated and kept in sync by comment — the same convention this codebase
already uses for `_TRAIT_MODS` ↔ `TraitMods` and `ENGINE_CATEGORIES` ↔
`GetEngineCategories`.

The duplication is small and asymmetric, which is what makes it affordable:

    this file MINTS and PUBLISHES.  The game server only VERIFIES.

Nothing here is ever shipped to a game server, and nothing there can produce a
token. That asymmetry is the entire point.

Why asymmetric signing at all, when `api_auth.py` signs its 30-day session
tokens with HMAC-SHA256 and that works fine: there, one party mints and the same
party verifies, so a shared secret is the simplest thing that works. Here the
verifier is *every self-hosted game server*, and an HMAC key that can verify is a
key that can mint. Ed25519 is what lets a host check a token is genuine while
being unable to forge one, so a compromised or malicious server can corrupt its
own universe and nothing else.

Document shape:

    config/mp_signing_keys
    {
        "current":  {"kid": "...", "pem": "-----BEGIN PRIVATE KEY-----...", "created_wc": 1788...},
        "previous": {"kid": "...", "pem": "...", "created_wc": 1788...},   # may be absent
        "rotated_at": "<iso8601>"
    }

**The private keys are at rest in Firestore**, and that is a deliberate choice
rather than an oversight. The alternatives are worse: a local file does not
survive a redeploy and breaks the moment there are two bot instances, and an
environment variable cannot rotate itself, which in practice means it never
rotates. Firestore is reachable only with the service-account credential the bot
already holds, so this is the same trust boundary as `.env` and the service
account JSON — not a new one. What must stay true is that no endpoint ever
returns the `pem` field; `jwks()` is the only publication path and it builds
public material from scratch.
"""

import base64
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from data.store import _db

log = logging.getLogger(__name__)

# ── Kept in sync with common/tokens.py in the Multiplayer-Server repo ────────
ALGORITHM = "EdDSA"
ISSUER = "boundlessmissions-accounts"
PLAYER_TOKEN_TTL_SECONDS = 15 * 60
SIGNING_KEY_LIFETIME_SECONDS = 90 * 24 * 3600
# ─────────────────────────────────────────────────────────────────────────────

# The JWKS is read by every game server on a schedule and by every joining
# client's server, so an uncached read here is a metered Firestore operation per
# join. Same reasoning as `data/policy.py`'s memoisation, and the same short TTL
# as a backstop for a rotation performed by another process.
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "doc": None}


class MpKeyError(RuntimeError):
    pass


def _doc():
    return _db.collection("config").document("mp_signing_keys")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key_id(public: Ed25519PublicKey) -> str:
    """A thumbprint, so the same key always has the same `kid`.

    Derived from the key rather than assigned, so the bot and a game server that
    has cached a JWKS agree about what to call a key without any shared naming
    scheme or coordination.
    """
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64u(hashlib.sha256(raw).digest())[:24]


def _new_key_entry(at_wc: float | None = None) -> dict:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "kid": key_id(priv.public_key()),
        "pem": pem,
        "created_wc": float(at_wc if at_wc is not None else time.time()),
    }


def _load_private(entry: dict) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(entry["pem"].encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise MpKeyError("stored multiplayer signing key is not Ed25519")
    return key


def _public_jwk(entry: dict) -> dict:
    pub = _load_private(entry).public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": entry["kid"],
        "x": _b64u(raw),
    }


def invalidate() -> None:
    with _cache_lock:
        _cache["doc"] = None
        _cache["at"] = 0.0


def _read(*, fresh: bool = False) -> dict:
    if not fresh:
        with _cache_lock:
            doc, at = _cache["doc"], float(_cache["at"])
        if doc is not None and (time.time() - at) < _CACHE_TTL:
            return doc
    snap = _doc().get()
    data = snap.to_dict() if snap.exists else {}
    with _cache_lock:
        _cache["doc"] = data
        _cache["at"] = time.time()
    return data


def get_keyset(*, at_wc: float | None = None) -> dict:
    """The live key set, creating or rotating it if due.

    Rotation is **lazy, on read**, rather than a scheduled job. That is the same
    shape `data/suspensions.py` uses for expiry and for the same reason: a
    sweeper is another moving part that can be down precisely when it matters,
    while resolving on read cannot drift from the thing it describes. The cost is
    that a bot which is never called never rotates — which is fine, because a key
    that signs nothing is a key nobody is verifying against.
    """
    now = float(at_wc if at_wc is not None else time.time())
    data = _read()
    current = data.get("current")

    if not current or "pem" not in current:
        current = _new_key_entry(now)
        data = {"current": current, "rotated_at": _now_iso()}
        _doc().set(data)
        invalidate()
        log.info("minted the first multiplayer signing key (kid=%s)", current["kid"])
        return data

    if now - float(current.get("created_wc", 0.0)) >= SIGNING_KEY_LIFETIME_SECONDS:
        # Promote current to previous and mint a new current. Both stay published
        # for the previous key's remaining life, so a token minted seconds before
        # this moment still verifies — which is the join outage the overlap
        # exists to prevent. The old `previous` is dropped here, which is what
        # actually retires it.
        data = {
            "current": _new_key_entry(now),
            "previous": current,
            "rotated_at": _now_iso(),
        }
        _doc().set(data)
        invalidate()
        log.info("rotated the multiplayer signing key (new kid=%s, previous=%s)",
                 data["current"]["kid"], current["kid"])
    return data


def jwks(*, at_wc: float | None = None) -> dict:
    """Public keys, in the shape a JWKS endpoint publishes.

    Public material only, built from the private keys rather than stored
    alongside them — so there is no path by which a stale or mistakenly-written
    private field could be served. Both keys appear whenever a previous one
    exists.
    """
    data = get_keyset(at_wc=at_wc)
    keys = [_public_jwk(data["current"])]
    prev = data.get("previous")
    if prev and "pem" in prev:
        keys.append(_public_jwk(prev))
    return {"keys": keys}


def mint_player_token(
    *,
    account_id: str,
    handle: str,
    universe_id: str,
    display_name: str = "",
    ttl_seconds: int = PLAYER_TOKEN_TTL_SECONDS,
    at_wc: float | None = None,
) -> tuple[str, int]:
    """Mint a token scoped to exactly one ``(account, universe)``.

    Returns ``(token, expires_wc)``.

    Three refusals, each closing something real:

    * **No handle, no token.** The handle is the immutable account name
      (`accounts.claim_username`, which refuses to change one already set).
      Contracts, disputes and combat reports all reference it, because display
      names are user-editable and renaming to match a trusted trader would
      otherwise be a trivial impersonation in a game where players sign binding
      agreements with strangers.
    * **No universe, no token.** Property is pinned to one universe, so a token
      naming only the account would let a host admit a player into a universe
      they hold nothing in — or let a hostile host replay a token it received
      into a different one.
    * **A 15-minute life.** Short TTL *is* the suspension mechanism: a suspended
      account stops being minted for, and every host learns within the TTL
      without any revocation push, list, or per-request round trip.
    """
    account_id = str(account_id or "").strip()
    handle = str(handle or "").strip()
    universe_id = str(universe_id or "").strip()
    if not account_id:
        raise MpKeyError("cannot mint a multiplayer token with no account id")
    if not handle:
        raise MpKeyError("cannot mint a multiplayer token with no immutable handle")
    if not universe_id:
        raise MpKeyError("cannot mint a multiplayer token with no universe")

    now = float(at_wc if at_wc is not None else time.time())
    data = get_keyset(at_wc=now)
    entry = data["current"]
    expires = int(now + ttl_seconds)

    claims = {
        "iss": ISSUER,
        "sub": account_id,
        "aud": universe_id,
        "iat": int(now),
        "nbf": int(now),
        "exp": expires,
        "jti": uuid.uuid4().hex,
        "handle": handle,
        "name": display_name or "",
    }
    token = jwt.encode(
        claims,
        entry["pem"],
        algorithm=ALGORITHM,
        headers={"kid": entry["kid"]},
    )
    return token, expires
