"""
api_server.py – FastAPI REST API for KSP mod ↔ Discord bot bridge.

Runs inside the bot process via uvicorn. All endpoints require a valid
session token (Authorization: Bearer <token>) except /auth/link.

No API keys, Firebase creds, or secrets are exposed to clients.
"""

import asyncio
import io
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import (
    FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import firestore

import settings
from config import cfg
from api_auth import (
    validate_link_code, create_session_token, verify_session_token,
    logout_all_devices, create_2fa_challenge, verify_2fa_challenge,
)
from api_models import (
    LinkRequest, LinkResponse, TwoFARequest,
    UserProfile,
    WeeklyMissionsResponse, Mission, MissionSelectRequest, MissionSelectResponse,
    ContractSummary, ContractListResponse, ContractAcceptResponse,
    PartCatalogUpload, PartCatalogResponse,
    CorpInfo, CorpListResponse, ContractCreateRequest, ContractReviewRequest,
    ContractDisputeRequest, RescueTarget,
    SubmissionResult, FlightSubmission, VesselSnapshot,
    Notification, NotificationsResponse,
    MarketplaceListResult, MarketplaceListing, MarketplaceListingsResponse,
)
from data.store import store, _db, _storage_bucket
from data import contracts as cdb
from data import mission_constraints as mc
from data import part_resolver as pr
from data import marketplace as mkt
from data import imports as imp

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=3))

# ── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Gene Kerman KSP Bridge API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket Notification Hub ───────────────────────────────────────────────

class NotificationHub:
    """Tracks live WebSocket connections per (guild_id, user_id) and pushes
    notifications to them. All public methods are coroutines and must run on
    the server event loop."""

    def __init__(self):
        self._conns: dict[tuple[int, int], set[WebSocket]] = {}

    async def connect(self, gid: int, uid: int, ws: WebSocket):
        await ws.accept()
        self._conns.setdefault((gid, uid), set()).add(ws)
        log.info("WS: user %d (guild %d) connected (%d live)", uid, gid, len(self._conns[(gid, uid)]))

    def disconnect(self, gid: int, uid: int, ws: WebSocket):
        conns = self._conns.get((gid, uid))
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            self._conns.pop((gid, uid), None)

    async def push(self, gid: int, uid: int, payload: dict):
        conns = self._conns.get((gid, uid))
        if not conns:
            return
        dead = []
        for ws in list(conns):
            try:
                await ws.send_json({"type": "notification", "notification": payload})
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)


_hub = NotificationHub()
_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_loop():
    """Capture the running event loop so the sync _create_notification helper can
    schedule pushes onto it from any context."""
    global _loop
    _loop = asyncio.get_running_loop()


def _push_notification(gid: int, uid: int, payload: dict):
    """Thread-safe fire-and-forget push of a notification to live sockets."""
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_hub.push(gid, uid, payload), _loop)
    except Exception as exc:
        log.warning("WS: failed to schedule push for user %d: %s", uid, exc)


# ── Auth Dependency ──────────────────────────────────────────────────────────

def _get_api_secret() -> str:
    # config.py refuses to start with a blank/default secret when the KSP API is
    # enabled, so this is always a real key here.
    return cfg.API_SECRET_KEY


# ── Rate limiting (link / 2FA brute-force defense) ───────────────────────────
#
# The link and 2FA endpoints accept short numeric codes, so they're the only
# guessable attack surface. A simple in-memory sliding window (per-IP and a
# global cap) keeps an attacker from sweeping the code space within a code's
# 3-minute life. In-process is sufficient: the bot is a single process.

_RATE_BUCKETS: dict[str, list[float]] = {}
_last_bucket_sweep: float = 0.0


def _sweep_rate_buckets(now: float):
    """Drop buckets with no recent hits so the dict can't grow unboundedly with
    one entry per IP ever seen. Cheap: runs at most every 5 minutes."""
    global _last_bucket_sweep
    if now - _last_bucket_sweep < 300:
        return
    _last_bucket_sweep = now
    for k in list(_RATE_BUCKETS.keys()):
        recent = [t for t in _RATE_BUCKETS[k] if now - t < 120]
        if recent:
            _RATE_BUCKETS[k] = recent
        else:
            del _RATE_BUCKETS[k]


def _rate_limit(key: str, max_hits: int, window: float):
    """Record a hit for `key`; raise 429 if it exceeds max_hits within window."""
    now = time.time()
    _sweep_rate_buckets(now)
    hits = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window]
    if len(hits) >= max_hits:
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a moment and try again.")
    hits.append(now)
    _RATE_BUCKETS[key] = hits


def _client_ip(request: Request) -> str:
    """The real client IP for rate limiting.

    X-Forwarded-For is honored ONLY when the request's direct peer is a
    configured trusted proxy — otherwise the header is attacker-controlled (each
    forged value would mint a fresh bucket and defeat per-IP limiting), so we use
    the raw socket peer. With trusted proxies set, walk the XFF chain from the
    right past any trusted hops; the first untrusted address is the client.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = cfg.API_TRUSTED_PROXIES
    if trusted and peer in trusted:
        chain = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(chain):
            if hop not in trusted:
                return hop
    return peer


def _guard_link_attempt(request: Request):
    """Throttle link/2FA attempts: per-IP and globally."""
    ip = _client_ip(request)
    # Tuned for headroom on a shared public IP without ever approaching a
    # feasible brute-force rate: even 60/min over a code's 3-min life is ~180
    # guesses against a 1,000,000-code space. Raise if many players share one IP.
    _rate_limit(f"link:{ip}", max_hits=10, window=60.0)    # 10 / min per IP
    _rate_limit("link:global", max_hits=60, window=60.0)   # 60 / min overall


async def get_current_user(authorization: str = Header(...)) -> dict:
    """Extract and validate the session token from Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]
    user = verify_session_token(token, _get_api_secret())
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user


# ── Auth Endpoints ───────────────────────────────────────────────────────────

def _issue_link_token(result: dict) -> LinkResponse:
    """Mint a session token for a validated identity and return the linked response."""
    token = create_session_token(
        result["guild_id"], result["user_id"], result["username"],
        _get_api_secret(),
    )
    return LinkResponse(
        status="ok",
        token=token,
        username=result["username"],
        guild_id=result["guild_id"],
        user_id=result["user_id"],
    )


async def _dm_2fa_code(user_id: int, otp: str) -> bool:
    """DM the 2FA code to the Discord user. Returns False if it couldn't be sent."""
    if not _bot_instance:
        return False
    try:
        import discord
        u = await _bot_instance.fetch_user(user_id)
        e = discord.Embed(
            title="🔐 KSP Login Verification",
            description=(
                f"Your KSP linking code:\n\n# `{otp}`\n\n"
                "Enter it in KSP to finish linking. Expires in 3 minutes.\n"
                "If you didn't try to link KSP, ignore this — someone may have "
                "your link code."
            ),
            color=discord.Color.orange(),
        )
        await u.send(embed=e)
        return True
    except Exception as exc:
        log.warning("Could not DM 2FA code to user %s: %s", user_id, exc)
        return False


@app.post("/api/v1/auth/link", response_model=LinkResponse)
async def auth_link(req: LinkRequest, request: Request):
    """Exchange a 6-digit link code for a session token (or a 2FA challenge)."""
    _guard_link_attempt(request)

    result = validate_link_code(req.code)
    if result is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link code")

    # 2FA off → link immediately.
    if not cfg.KSP_2FA_ENABLED:
        return _issue_link_token(result)

    # 2FA on → DM a one-time code and wait for /auth/link/2fa. The link code is
    # already consumed, so a failed DM means this attempt can't proceed.
    challenge_id, otp = create_2fa_challenge(
        result["guild_id"], result["user_id"], result["username"])
    sent = await _dm_2fa_code(int(result["user_id"]), otp)
    if not sent:
        raise HTTPException(
            status_code=502,
            detail="Couldn't DM your verification code. Enable DMs from server "
                   "members in Discord, then request a new link code.",
        )

    log.info("KSP: 2FA challenge issued for %s", result["username"])
    return LinkResponse(status="2fa_required", challenge_id=challenge_id)


@app.post("/api/v1/auth/link/2fa", response_model=LinkResponse)
async def auth_link_2fa(req: TwoFARequest, request: Request):
    """Complete linking by submitting the code DM'd to the user."""
    _guard_link_attempt(request)

    result = verify_2fa_challenge(req.challenge_id, req.code)
    if result is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    log.info("KSP: 2FA verified, linking %s", result["username"])
    return _issue_link_token(result)


@app.get("/api/v1/auth/verify", response_model=UserProfile)
async def auth_verify(user: dict = Depends(get_current_user)):
    """Validate session token and return user profile."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    u = store.get_user(gid, uid)

    return UserProfile(
        user_id=user["user_id"],
        username=user["username"],
        guild_id=user["guild_id"],
        xp=u.get("xp", 0),
        level=u.get("level", 0),
        balance=u.get("balance", 0),
        messages=u.get("messages", 0),
        unlocked_levels=u.get("unlocked_levels", []),
        currency_name=settings.CURRENCY_NAME,
    )


@app.post("/api/v1/auth/logout_all")
async def auth_logout_all(user: dict = Depends(get_current_user)):
    """Log the current user out of every device.

    The user's own privacy control for an account left linked somewhere else.
    Bumps their token version so every session token — including this caller's —
    is rejected from here on; each device drops to its unlinked state on its next
    request. Not an admin action: a user can only log out their own sessions.
    """
    new_version = logout_all_devices(user["user_id"])
    log.info("KSP: %s logged out of all devices", user["username"])
    return {"success": True, "token_version": new_version}


# ── User Profile ─────────────────────────────────────────────────────────────

@app.get("/api/v1/user/profile", response_model=UserProfile)
async def user_profile(user: dict = Depends(get_current_user)):
    """Get the current user's profile (balance, XP, level)."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    u = store.get_user(gid, uid)

    return UserProfile(
        user_id=user["user_id"],
        username=user["username"],
        guild_id=user["guild_id"],
        xp=u.get("xp", 0),
        level=u.get("level", 0),
        balance=u.get("balance", 0),
        messages=u.get("messages", 0),
        unlocked_levels=u.get("unlocked_levels", []),
        currency_name=settings.CURRENCY_NAME,
    )


# ── Weekly Missions ──────────────────────────────────────────────────────────

def _classification_ref(week_key: str):
    """Firestore ref for cached mission classifications."""
    return _db.collection("mission_classifications").document(week_key)


async def _classify_missions(missions: list[dict], week_key: str) -> list[dict]:
    """
    AI-classify each mission as 'craft_build' or 'active_vessel' with
    required_situation and required_body. Results are cached in Firestore
    so AI is only called once per week.
    """
    # Check cache first
    ref = _classification_ref(week_key)
    snap = ref.get()
    if snap.exists:
        cached = snap.to_dict().get("classifications", {})
        if cached:
            for m in missions:
                key = str(m["id"])
                if key in cached:
                    m["mission_type"] = cached[key].get("mission_type", "active_vessel")
                    m["required_situation"] = cached[key].get("required_situation")
                    m["required_body"] = cached[key].get("required_body")
            return missions

    # No cache — run AI classification
    try:
        from cogs.screenshots import _client as gemini_client, _MODEL
    except Exception:
        gemini_client = None

    if not gemini_client:
        # No AI — fall back to heuristic classification
        return _classify_heuristic(missions)

    # Build AI prompt
    mission_list = "\n".join(
        f'{m["id"]}. {m["desc_en"]} (category: {m["category"]}, difficulty: {m["difficulty"]})'
        for m in missions
    )

    prompt = (
        "You are classifying KSP (Kerbal Space Program) missions for a mod.\n\n"
        "For each mission, determine:\n"
        "1. mission_type: 'craft_build' (vessel must be shown in VAB/SPH editor) "
        "or 'active_vessel' (vessel must be in flight, at the right place)\n"
        "2. required_situation: The KSP vessel situation needed. "
        "Options: ORBITING, LANDED, SPLASHED, FLYING, SUB_ORBITAL, ESCAPING, DOCKED, null\n"
        "3. required_body: The celestial body name (Kerbin, Mun, Minmus, Duna, Eve, Jool, etc.) or null\n\n"
        "Rules:\n"
        "- 'construction' category missions that say 'build' are 'craft_build'\n"
        "- Missions about orbiting, landing, flying, returning are 'active_vessel'\n"
        "- If a mission says 'orbit X', required_situation = 'ORBITING', required_body = X\n"
        "- If a mission says 'land on X', required_situation = 'LANDED', required_body = X\n"
        "- 'flyby' missions: required_situation = 'ESCAPING' or 'SUB_ORBITAL', required_body = target\n"
        "- 'dock' missions: required_situation = 'ORBITING' (docking happens in orbit)\n"
        "- If no specific body/situation, set to null\n\n"
        f"Missions:\n{mission_list}\n\n"
        "Return ONLY valid JSON — an array of objects:\n"
        '[{"id": 1, "mission_type": "...", "required_situation": "...", "required_body": "..."}]'
    )

    from google.genai import types
    import json

    try:
        response = gemini_client.models.generate_content(
            model=_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=2048),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        classifications = json.loads(raw.strip())
    except Exception as exc:
        log.error("AI mission classification failed: %s", exc)
        return _classify_heuristic(missions)

    # Apply classifications and build cache
    cache = {}
    cls_map = {c["id"]: c for c in classifications}

    for m in missions:
        cls = cls_map.get(m["id"], {})
        m["mission_type"] = cls.get("mission_type", "active_vessel")
        m["required_situation"] = cls.get("required_situation")
        m["required_body"] = cls.get("required_body")
        cache[str(m["id"])] = {
            "mission_type": m["mission_type"],
            "required_situation": m.get("required_situation"),
            "required_body": m.get("required_body"),
        }

    # Save to Firestore cache
    ref.set({"classifications": cache, "classified_at": datetime.now(timezone.utc).isoformat()})
    log.info("AI classified %d missions for week %s", len(cache), week_key)

    return missions


def _classify_heuristic(missions: list[dict]) -> list[dict]:
    """Fallback heuristic when AI is unavailable."""
    build_keywords = ["build", "construct", "assemble", "deploy a relay", "deploy a communication"]
    for m in missions:
        desc_lower = m["desc_en"].lower()
        if m["category"] == "construction" or any(kw in desc_lower for kw in build_keywords):
            m["mission_type"] = "craft_build"
        else:
            m["mission_type"] = "active_vessel"

        m["required_body"] = None
        for body in settings.KNOWN_CELESTIAL_BODIES:
            if body.lower() in desc_lower:
                m["required_body"] = body
                break

        m["required_situation"] = None
        if "orbit" in desc_lower:
            m["required_situation"] = "ORBITING"
        elif "land" in desc_lower:
            m["required_situation"] = "LANDED"
        elif "flyby" in desc_lower:
            m["required_situation"] = "SUB_ORBITAL"
        elif "dock" in desc_lower:
            m["required_situation"] = "ORBITING"

    return missions


# ── Single-contract classification (for human-issued contracts) ──────────

_BUILD_KEYWORDS = [
    "build", "construct", "assemble", "design", "create", "make",
    "deploy a relay", "deploy a communication", "station with",
]
_FLIGHT_KEYWORDS = [
    "orbit", "land on", "fly to", "reach", "dock", "rendezvous",
    "return", "flyby", "intercept", "capture", "eva", "splashdown",
]


def _classify_text_heuristic(mission_text: str) -> dict:
    """Classify a single mission description using keyword heuristics."""
    text_lower = mission_text.lower()

    is_build = any(kw in text_lower for kw in _BUILD_KEYWORDS)
    is_flight = any(kw in text_lower for kw in _FLIGHT_KEYWORDS)

    # Build keywords without flight keywords = craft_build.
    # Both present = flight takes priority ("build and fly to orbit" = active_vessel).
    if is_build and not is_flight:
        mission_type = "craft_build"
    else:
        mission_type = "active_vessel"

    required_body = None
    for body in settings.KNOWN_CELESTIAL_BODIES:
        if body.lower() in text_lower:
            required_body = body
            break

    required_situation = None
    if mission_type == "active_vessel":
        if "orbit" in text_lower:
            required_situation = "ORBITING"
        elif "land" in text_lower:
            required_situation = "LANDED"
        elif "flyby" in text_lower:
            required_situation = "SUB_ORBITAL"
        elif "dock" in text_lower:
            required_situation = "ORBITING"

    return {
        "mission_type": mission_type,
        "required_situation": required_situation,
        "required_body": required_body,
    }


# ── KSP part catalog + part-name resolution ──────────────────────────────────
#
# Resolving a mission's loose part mention ("the Thud engine", a typo'd "thudd")
# to a real installed part needs the player's actual part list. The KSP client
# uploads that catalog (hash-gated); we cache it in memory and persist it to
# Firestore so it survives a bot restart. Resolutions (and the AI tie-breaks they
# sometimes need) are cached per (catalog-hash, mention) so a fetch costs nothing
# after the first.

_PART_CATALOGS: dict[str, dict] = {}        # "gid:uid" -> {"hash":..., "parts":[...]}
_RESOLVE_CACHE: dict[tuple, str | None] = {}  # (catalog_hash, loose_lower) -> name|None


def _catalog_key(gid: int, uid: int) -> str:
    return f"{gid}:{uid}"


def _catalog_doc(gid: int, uid: int):
    return _db.collection("guilds").document(str(gid)).collection("part_catalogs").document(str(uid))


def _get_user_catalog(gid: int, uid: int) -> dict | None:
    """The requesting user's uploaded catalog, loading from Firestore on a cold cache."""
    key = _catalog_key(gid, uid)
    cat = _PART_CATALOGS.get(key)
    if cat is not None:
        return cat
    try:
        snap = _catalog_doc(gid, uid).get()
        if snap.exists:
            cat = snap.to_dict()
            _PART_CATALOGS[key] = cat
            return cat
    except Exception as exc:
        log.warning("Failed to load part catalog for %s: %s", key, exc)
    return None


def _ai_resolve_part(mission_text: str):
    """Build an ai_resolver(loose, candidates)->name|None bound to this mission's
    text, or None when no AI is configured."""
    try:
        from cogs.screenshots import _client as gemini_client, _MODEL
    except Exception:
        gemini_client = None
    if not gemini_client:
        return None

    from google.genai import types
    import json

    def _resolver(loose: str, candidates: list[dict]) -> str | None:
        if not candidates:
            return None
        listing = "\n".join(f"- {c.get('name')} | {c.get('title')}" for c in candidates[:12])
        prompt = (
            "A KSP mission has a part restriction mentioning a part by an informal "
            "or possibly mistyped name. Pick which installed part it refers to.\n\n"
            f"Mission: \"{mission_text}\"\n"
            f"Mentioned part: \"{loose}\"\n\n"
            "Installed candidates (internal_name | display_title):\n"
            f"{listing}\n\n"
            "Reply with ONLY the exact internal_name of the best match, or NONE if "
            "none clearly fits."
        )
        try:
            resp = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=64),
            )
            ans = (resp.text or "").strip().strip("`").splitlines()[0].strip()
            if not ans or ans.upper() == "NONE":
                return None
            return ans
        except Exception as exc:
            log.warning("Gemini part resolution failed for %r: %s", loose, exc)
            return None

    return _resolver


def _resolve_constraints(constraints: dict | None, gid: int, uid: int,
                         mission_text: str) -> dict | None:
    """Add resolved internal part names to a constraints dict using the user's
    catalog (deterministic fuzzy + AI tie-break, both cached). Returns the
    constraints unchanged when there's nothing to resolve or no catalog."""
    if mc.is_empty(constraints):
        return constraints
    if not (constraints.get("forbidden_parts") or constraints.get("required_parts")):
        return constraints
    cat = _get_user_catalog(gid, uid)
    if not cat or not cat.get("parts"):
        return constraints  # no catalog yet → loose matching only

    chash = cat.get("hash") or pr.catalog_hash(cat["parts"])
    ai = _ai_resolve_part(mission_text)

    def _cached_resolver(loose: str) -> str | None:
        ck = (chash, loose.lower())
        if ck in _RESOLVE_CACHE:
            return _RESOLVE_CACHE[ck]
        name = pr.resolve_part(loose, cat["parts"], ai)
        _RESOLVE_CACHE[ck] = name
        return name

    return mc.resolve_parts(constraints, _cached_resolver)


async def _classify_single_contract(gid: int, contract_id: str, mission_text: str) -> dict:
    """
    Classify a single contract's mission text. Uses AI if available,
    falls back to heuristic. Caches result back to the contract doc.
    """
    # Try AI first
    try:
        from cogs.screenshots import _client as gemini_client, _MODEL
    except Exception:
        gemini_client = None

    if gemini_client:
        from google.genai import types
        import json

        prompt = (
            "Classify this KSP mission for a mod. The mission text may be in English or Turkish.\n\n"
            f"Mission: \"{mission_text}\"\n\n"
            "Determine:\n"
            "1. mission_type: 'craft_build' (design/build a vessel in VAB/SPH) "
            "or 'active_vessel' (fly vessel to specific place/situation)\n"
            "2. required_situation: ORBITING, LANDED, SPLASHED, FLYING, SUB_ORBITAL, ESCAPING, or null\n"
            "3. required_body: celestial body name or null\n"
            "4. constraints: part-usage restrictions stated in the text (a 'mission limit'). "
            "Leave every list empty if the text states no restriction. Use these keys, each a list of strings:\n"
            "   - forbidden_parts / required_parts: specific part names, e.g. \"Thud\", \"Mainsail\"\n"
            "   - forbidden_propellants / required_propellants: fuel/resource names, "
            "e.g. \"LqdHe3\", \"LiquidFuel\", \"XenonGas\", \"MonoPropellant\", \"SolidFuel\"\n"
            "   - forbidden_engine_categories / required_engine_categories: one or more of "
            "[nuclear, ion, solid, chemical, electric, monoprop, rcs]\n"
            "   - forbidden_part_categories / required_part_categories: one or more of "
            "[heatshield, parachute, solarpanel, wheel, ladder, reactionwheel, rtg]\n"
            "   - max_parts / min_parts: integer part-count limits, or null. "
            "'at most/up to N parts' => max_parts N; 'fewer than N' => max_parts N-1; "
            "'at least N' => min_parts N; 'more than N' => min_parts N+1.\n"
            "   - max_dv / min_dv: vacuum delta-v (Δv) limits in m/s, or null. "
            "Convert km/s to m/s (3.5 km/s => 3500). 'at least 3000 m/s of delta-v' => "
            "min_dv 3000; 'no more than 5000 m/s dv' => max_dv 5000.\n\n"
            "Constraint rules:\n"
            "- 'must use / only / powered by X' => required_*. "
            "'can't use / doesn't use / does not use / no / without / X-less' => forbidden_*.\n"
            "- Negation flips intent: 'doesn't use deuterium-powered engines' => "
            "forbidden_propellants ['LqdDeuterium'] (NOT required). Never put the same item "
            "in both a forbidden and a required list.\n"
            "- 'nuclear/atomic/NTR/NERV engine' => engine category 'nuclear'. 'ion' => 'ion'. "
            "'SRB/solid booster' => 'solid'.\n"
            "- 'heatshield-less / no heat shield' => forbidden_part_categories ['heatshield'].\n"
            "- 'Lqd He3 / helium-3 powered' => required_propellants ['LqdHe3'].\n"
            "- When the text names a specific part (e.g. 'Vector', 'Mainsail', 'Thud'), copy that "
            "name VERBATIM into required_parts/forbidden_parts — never translate it to a real-world "
            "or 'equivalent' name (do NOT turn 'Vector' into 'SSME', or 'Mainsail' into 'RS-68'), and "
            "do NOT also add an engine category for that named part. Only set an engine category when "
            "the text names a general *kind* of engine (e.g. 'nuclear engine', 'any ion thruster'). "
            "Map a fuel to a propellant.\n\n"
            "Return ONLY valid JSON:\n"
            '{"mission_type": "...", "required_situation": "...", "required_body": "...", '
            '"constraints": {"forbidden_parts": [], "required_parts": [], '
            '"forbidden_propellants": [], "required_propellants": [], '
            '"forbidden_engine_categories": [], "required_engine_categories": [], '
            '"forbidden_part_categories": [], "required_part_categories": [], '
            '"max_parts": null, "min_parts": null, "max_dv": null, "min_dv": null}}'
        )

        try:
            response = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=512),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            result = json.loads(raw.strip())
            # AI is authoritative for constraints: trust its decision, including an
            # all-empty result ("no limits"). The heuristic only steps in when the
            # AI is unavailable or errors (the fallback branches below).
            result["constraints"] = mc.normalize(result.get("constraints"))
            log.info("AI classified contract %s: %s%s", contract_id, result.get("mission_type"),
                     "" if mc.is_empty(result["constraints"]) else f" + limits ({mc.summary_line(result['constraints'])})")
        except Exception as exc:
            log.error("AI single-contract classification failed: %s", exc)
            result = _classify_text_heuristic(mission_text)
            result["constraints"] = mc.extract_heuristic(mission_text)
    else:
        result = _classify_text_heuristic(mission_text)
        result["constraints"] = mc.extract_heuristic(mission_text)

    # Cache result back to the contract document
    try:
        cdb.update_contract(gid, contract_id,
            mission_type=result.get("mission_type", "active_vessel"),
            required_situation=result.get("required_situation"),
            required_body=result.get("required_body"),
            constraints=result.get("constraints"),
        )
    except Exception as exc:
        log.error("Failed to cache classification for %s: %s", contract_id, exc)

    return result


@app.get("/api/v1/missions/weekly", response_model=WeeklyMissionsResponse)
async def get_weekly_missions(user: dict = Depends(get_current_user)):
    """Get the current week's 20 missions with AI classification."""
    from cogs.weeklymissions import _week_key, _week_bounds, _is_locked, _load_missions, _generate_missions

    gid = int(user["guild_id"])
    now = datetime.now(TZ)
    wk = _week_key(now)

    missions, _ = _load_missions(gid, wk)
    if not missions:
        missions = _generate_missions(wk, settings.WEEKLY_MISSIONS_COUNT)

    # Classify missions (cached — AI runs at most once per week)
    missions = await _classify_missions(missions, wk)

    _, week_end = _week_bounds(now)
    closes_at = (week_end - timedelta(days=1)).isoformat()

    return WeeklyMissionsResponse(
        week_key=wk,
        missions=[Mission(**m) for m in missions],
        is_locked=_is_locked(now),
        closes_at=closes_at,
    )


@app.post("/api/v1/missions/select", response_model=MissionSelectResponse)
async def select_mission(req: MissionSelectRequest, user: dict = Depends(get_current_user)):
    """Accept a weekly mission — creates a contract in Firestore."""
    from cogs.weeklymissions import (
        _week_key, _week_bounds, _is_locked, _load_missions, _generate_missions,
        _has_selected, _save_selection,
    )
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    now = datetime.now(TZ)
    wk = _week_key(now)

    # Locked?
    if _is_locked(now):
        return MissionSelectResponse(success=False, message="Mission selection is locked (Sunday).")

    # Has corp?
    corp = _get_corp(gid, uid)
    if not corp:
        return MissionSelectResponse(success=False, message="You need a corporation first! Use /g corpsetup in Discord.")

    # Already selected?
    if _has_selected(gid, wk, uid, req.mission_id):
        return MissionSelectResponse(success=False, message="You already selected this mission.")

    # Find the mission
    missions, _ = _load_missions(gid, wk)
    if not missions:
        missions = _generate_missions(wk, settings.WEEKLY_MISSIONS_COUNT)

    # Ensure classification is loaded
    missions = await _classify_missions(missions, wk)

    mission = next((m for m in missions if m["id"] == req.mission_id), None)
    if mission is None:
        return MissionSelectResponse(success=False, message="Mission not found.")

    # Create contract
    _, week_end = _week_bounds(now)
    due = (week_end - timedelta(days=1)).strftime("%Y-%m-%d")

    # Use bot user ID as issuer — we need to get it from somewhere
    # The bot instance is stored globally after startup
    bot_user_id = _get_bot_user_id()

    c = cdb.create_contract(
        guild_id=gid,
        issuer_id=bot_user_id,
        issuer_name="Gene Kerman",
        contractor_id=uid,
        contractor_name=user["username"],
        mission=mission["desc_en"],
        payment=mission["coins"],
        fine=mission["fine"],
        due_date=due,
    )
    # Store classification on the contract so KSP can enforce rules. Part-limit
    # constraints are derived from the mission text (heuristic — no extra AI call).
    weekly_constraints = mission.get("constraints") or mc.extract_heuristic(mission.get("desc_en", ""))
    cdb.update_contract(gid, c["contract_id"],
        status=cdb.ACTIVE,
        mission_type=mission.get("mission_type", "active_vessel"),
        required_situation=mission.get("required_situation"),
        required_body=mission.get("required_body"),
        constraints=weekly_constraints,
    )

    # Save selection
    _save_selection(gid, wk, uid, req.mission_id)

    # Create a notification for the user
    _create_notification(gid, uid, "mission_accepted",
                         f"Mission #{req.mission_id} Accepted",
                         f"Weekly mission accepted. Due: {due}. Reward: +{mission['coins']} KCoins, +{mission['xp']} XP.",
                         {"contract_id": c["contract_id"], "mission_id": req.mission_id})

    log.info("KSP: %s accepted weekly mission #%d", user["username"], req.mission_id)

    return MissionSelectResponse(
        success=True,
        contract_id=c["contract_id"],
        message=f"Mission #{req.mission_id} accepted!",
    )


# ── Contracts ────────────────────────────────────────────────────────────────

@app.post("/api/v1/parts/catalog", response_model=PartCatalogResponse)
async def upload_part_catalog(req: PartCatalogUpload, user: dict = Depends(get_current_user)):
    """Receive the KSP client's installed part list so the bot can resolve loosely
    typed part mentions in mission limits. Hash-gated: an unchanged catalog is a
    no-op. Stored in memory and persisted to Firestore to survive restarts."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    key = _catalog_key(gid, uid)

    existing = _get_user_catalog(gid, uid)
    if existing and existing.get("hash") == req.hash and existing.get("parts"):
        return PartCatalogResponse(success=True, stored=False, parts=len(existing["parts"]))

    # Keep only the two fields we use, capped to a sane size.
    parts = [
        {"name": str(p.get("name", "")), "title": str(p.get("title", ""))}
        for p in (req.parts or []) if p.get("name") or p.get("title")
    ][:8000]
    cat = {"hash": req.hash, "parts": parts}
    _PART_CATALOGS[key] = cat
    # Invalidate cached resolutions for any previous catalog of this user.
    for ck in [k for k in _RESOLVE_CACHE if k[0] != req.hash]:
        _RESOLVE_CACHE.pop(ck, None)
    try:
        _catalog_doc(gid, uid).set(cat)
    except Exception as exc:
        log.warning("Could not persist part catalog for %s (memory only): %s", key, exc)

    log.info("Stored part catalog for %s: %d parts (hash %s)", key, len(parts), req.hash[:8])
    return PartCatalogResponse(success=True, stored=True, parts=len(parts))


@app.get("/api/v1/contracts/active", response_model=ContractListResponse)
async def get_active_contracts(user: dict = Depends(get_current_user)):
    """Get all active contracts for the current user."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])
    bot_uid = str(_get_bot_user_id())

    col = cdb._col(gid)
    active_statuses = [cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.COMPLETED]
    contracts = []

    for doc in col.where("status", "in", active_statuses).stream():
        c = doc.to_dict()
        if c.get("contractor_id") == uid or c.get("issuer_id") == uid:
            # Auto-classify if missing (human-issued or old contracts)
            mission_type = c.get("mission_type")
            req_sit = c.get("required_situation")
            req_body = c.get("required_body")
            constraints = c.get("constraints")

            # Rescue contracts carry an explicit type — never AI-classify them.
            if not mission_type and c.get("mission_type") != cdb.RESCUE:
                cls = await _classify_single_contract(gid, c["contract_id"], c["mission"])
                mission_type = cls.get("mission_type", "active_vessel")
                req_sit = cls.get("required_situation")
                req_body = cls.get("required_body")
                constraints = cls.get("constraints")
            elif "constraints" not in c and c.get("mission_type") != cdb.RESCUE:
                # Legacy contract from before constraint extraction existed (no
                # constraints field at all) — derive cheaply so it still gets
                # editor/submit enforcement. A contract the AI already classified
                # keeps its stored decision, including a deliberate "no limits"
                # (empty dict) — we must not re-derive over the AI's call here.
                constraints = mc.extract_heuristic(c.get("mission", ""))
            # Resolve loose part mentions against this user's installed catalog
            # so the client filters/checks the exact part, not a fragile substring.
            if not mc.is_empty(constraints):
                constraints = _resolve_constraints(constraints, gid, uid, c.get("mission", ""))
            # Don't ship an all-empty constraints object to the client.
            if mc.is_empty(constraints):
                constraints = None

            rescue_target = None
            rescue_kerbals = []
            is_modded_target = False
            rescue_vessel_node_url = None
            if c.get("mission_type") == cdb.RESCUE:
                rt = c.get("rescue_target") or {}
                rescue_target = RescueTarget(**rt) if rt else None
                rescue_kerbals = c.get("rescue_kerbals", [])
                is_modded_target = bool(rt.get("is_modded"))
                # Only the rescuer (contractor) gets the wreck node, so their client
                # can spawn/respawn the stranded vessel on demand after accepting.
                if c.get("contractor_id") == uid:
                    rescue_vessel_node_url = c.get("rescue_vessel_node_url")

            contracts.append(ContractSummary(
                contract_id=c["contract_id"],
                mission=c["mission"],
                issuer_name=c.get("issuer_name", "Unknown"),
                contractor_name=c.get("contractor_name", "Unknown"),
                payment=c["payment"],
                fine=c["fine"],
                due_date=c["due_date"],
                status=c["status"],
                created_at=c.get("created_at"),
                is_bot_issued=(c.get("issuer_id") == bot_uid),
                is_outgoing=(c.get("issuer_id") == uid),
                modlist=c.get("modlist"),
                mission_type=mission_type,
                required_situation=req_sit,
                required_body=req_body,
                constraints=constraints,
                rescue_target=rescue_target,
                rescue_kerbals=rescue_kerbals,
                is_modded_target=is_modded_target,
                rescue_vessel_node_url=rescue_vessel_node_url,
                flag_preview_url=c.get("flag_preview_url"),
            ))

    # Sort newest first
    contracts.sort(key=lambda c: c.created_at or "", reverse=True)

    return ContractListResponse(contracts=contracts)


@app.get("/api/v1/contracts/incoming", response_model=ContractListResponse)
async def get_incoming_contracts(user: dict = Depends(get_current_user)):
    """Get pending contracts where this user is the contractor."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    col = cdb._col(gid)
    contracts = []

    for doc in col.where("status", "==", cdb.PENDING).stream():
        c = doc.to_dict()
        if c.get("contractor_id") == uid:
            contracts.append(ContractSummary(
                contract_id=c["contract_id"],
                mission=c["mission"],
                issuer_name=c.get("issuer_name", "Unknown"),
                contractor_name=c.get("contractor_name", "Unknown"),
                payment=c["payment"],
                fine=c["fine"],
                due_date=c["due_date"],
                status=c["status"],
                created_at=c.get("created_at"),
                is_bot_issued=False,
                mission_type=c.get("mission_type", "active_vessel"),
                flag_preview_url=c.get("flag_preview_url"),
            ))

    return ContractListResponse(contracts=contracts)


@app.post("/api/v1/contracts/{contract_id}/accept", response_model=ContractAcceptResponse)
async def accept_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Accept a pending contract."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if c.get("contractor_id") != uid:
        raise HTTPException(status_code=403, detail="Not your contract")

    if c.get("status") != cdb.PENDING:
        return ContractAcceptResponse(success=False, message="Contract is not pending.")

    cdb.update_contract(gid, contract_id, status=cdb.ACTIVE)
    log.info("KSP: %s accepted contract %s", user["username"], contract_id)

    # Tell the issuer their offer was accepted so their in-game contract list
    # refreshes live (the contractor already sees the result of their own click).
    issuer_id = int(c["issuer_id"])
    if issuer_id != _get_bot_user_id():
        _create_notification(
            gid, issuer_id, "contract_accepted",
            "🤝 Contract Accepted",
            f"{user['username']} accepted your contract \"{c['mission'][:80]}\".",
            data={"contract_id": contract_id},
        )

    # Rescue: hand the rescuer the wreck snapshot + target so their client can
    # spawn the stranded vessel at the chosen orbit/surface. The issuer's vessel
    # was already removed at creation, so nothing to do on their side here.
    if c.get("mission_type") == cdb.RESCUE:
        rt = c.get("rescue_target") or {}
        return ContractAcceptResponse(
            success=True, message="Rescue accepted! Spawning the stranded vessel.",
            rescue_vessel_node_url=c.get("rescue_vessel_node_url"),
            rescue_target=RescueTarget(**rt) if rt else None,
            rescue_kerbals=c.get("rescue_kerbals", []),
        )

    return ContractAcceptResponse(success=True, message="Contract accepted!")


@app.post("/api/v1/contracts/{contract_id}/review", response_model=ContractAcceptResponse)
async def review_submission(contract_id: str, req: ContractReviewRequest,
                            user: dict = Depends(get_current_user)):
    """Issuer reviews a submitted contract: approve (→ completed, pay contractor)
    or refuse (→ disputed). Mirrors the Discord ContractReviewView buttons so the
    review can be done from the KSP mod without switching to Discord."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    # Only the issuer reviews submissions (the contractor is the submitter).
    if str(c.get("issuer_id")) != uid:
        raise HTTPException(status_code=403, detail="Only the contract issuer can review submissions.")

    if c.get("status") != cdb.SUBMITTED:
        return ContractAcceptResponse(success=False, message="Contract is not awaiting review.")

    contractor_id = int(c["contractor_id"])

    if req.approve:
        cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                            completed_at=datetime.utcnow().isoformat())
        await store.add_balance(gid, contractor_id, c["payment"])
        c["status"] = cdb.COMPLETED
        _create_notification(
            gid, contractor_id, "review_result",
            "✅ Mission Approved!",
            f"Your submission for \"{c['mission'][:80]}\" was approved. "
            f"+{c['payment']} {settings.CURRENCY_SYMBOL} paid.",
            {"contract_id": contract_id},
        )
        # Best-effort Discord DM so the contractor is notified outside the game too.
        if _bot_instance:
            try:
                import discord
                from i18n import t
                contractor = await _bot_instance.fetch_user(contractor_id)
                ne = discord.Embed(
                    title=f"✅ {t(gid, 'ct.accepted')}",
                    description=t(gid, 'ct.accepted_desc',
                                 payment=c['payment'], sym=settings.CURRENCY_SYMBOL),
                    color=discord.Color.green())
                await contractor.send(embed=ne)
            except Exception as exc:
                log.warning("Could not DM contractor after KSP review-approve: %s", exc)
        # Rescue: return the kerbals to the issuer. Restore their original names,
        # queue the rescue craft for live import into the issuer's save, and tell
        # the rescuer's client to remove the craft it just handed over.
        if c.get("mission_type") == cdb.RESCUE:
            await _deliver_rescue_craft(gid, contract_id, c)
            _create_notification(
                gid, contractor_id, "rescue_craft_removed",
                "🚀 Rescue Craft Transferred",
                "Your rescue craft and the rescued kerbals were delivered to the issuer.",
                {"contract_id": contract_id},
            )
        # Flag-design: deliver the full-res flag to the issuer's in-game picker.
        if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
            imp.enqueue(gid, int(c["issuer_id"]), source="flag", ref_id=contract_id,
                        craft_name=c["mission"], flag_url=c["flag_fullres_url"],
                        craft_filename=c.get("flag_filename") or "flag.png")
            _create_notification(
                gid, int(c["issuer_id"]), "flag_delivered",
                "🚩 Flag Delivered",
                "Your custom flag is queued — open KSP at the Space Center to install it "
                "into your flag picker.",
                {"contract_id": contract_id},
            )
        log.info("KSP: %s approved submission for contract %s", user["username"], contract_id)
        return ContractAcceptResponse(success=True, message="Submission approved! Payment released.")

    # Refuse → dispute. Hand the dispute flow back to Discord (DisputeView is
    # Discord-only), so the contractor can settle / request more time / pay fine.
    cdb.update_contract(gid, contract_id, status=cdb.DISPUTED)
    c["status"] = cdb.DISPUTED
    _create_notification(
        gid, contractor_id, "review_result",
        "⚠️ Submission Refused",
        f"Your submission for \"{c['mission'][:80]}\" was refused. "
        f"Check Discord to resolve the dispute.",
        {"contract_id": contract_id},
    )
    if _bot_instance:
        try:
            import discord
            from i18n import t
            from cogs.contract_views import DisputeView
            contractor = await _bot_instance.fetch_user(contractor_id)
            de = discord.Embed(
                title=f"⚠️ {t(gid, 'ct.disputed')}",
                description=t(gid, 'ct.disputed_desc'),
                color=discord.Color.orange())
            await contractor.send(embed=de, view=DisputeView(contract_id, gid))
        except Exception as exc:
            log.warning("Could not DM contractor after KSP review-refuse: %s", exc)
    log.info("KSP: %s refused submission for contract %s", user["username"], contract_id)
    return ContractAcceptResponse(success=True, message="Submission refused. Dispute opened on Discord.")


@app.post("/api/v1/contracts/{contract_id}/dispute", response_model=ContractAcceptResponse)
async def resolve_dispute(contract_id: str, req: ContractDisputeRequest,
                          user: dict = Depends(get_current_user)):
    """Contractor resolves a refused (disputed) submission from the KSP mod,
    mirroring the Discord DisputeView buttons: settle / more_time / pay_fine / sue.

    Actions needing the other party's approval (settle, more_time on human
    contracts) hand off to the existing Discord approval views, exactly like
    review_submission does for the dispute itself."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    # Only the contractor (whose submission was refused) drives the dispute.
    if str(c.get("contractor_id")) != uid:
        raise HTTPException(status_code=403, detail="Only the contractor can resolve this dispute.")

    if c.get("status") != cdb.DISPUTED:
        return ContractAcceptResponse(success=False, message="Contract is not in dispute.")

    action = (req.action or "").lower()
    issuer_id = int(c["issuer_id"])
    contractor_id = int(c["contractor_id"])
    is_bot_issued = issuer_id == _get_bot_user_id()

    # ── Pay Fine ── deduct the fine and release escrow; closes the contract.
    if action == "pay_fine":
        bal = store.get_user(gid, contractor_id)["balance"]
        if bal < c["fine"]:
            return ContractAcceptResponse(success=False, message="Insufficient balance to pay the fine.")
        await store.add_balance(gid, contractor_id, -c["fine"])
        await store.add_balance(gid, issuer_id, c["fine"] + c["payment"])
        cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                            completed_at=datetime.utcnow().isoformat())
        if not is_bot_issued:
            _create_notification(
                gid, issuer_id, "review_result", "💰 Fine Paid",
                f"{c['contractor_name']} paid the fine for \"{c['mission'][:80]}\". "
                f"+{c['fine'] + c['payment']} {settings.CURRENCY_SYMBOL}.",
                {"contract_id": contract_id},
            )
        # Rescue: the rescuer gave up (paid the fine) — kerbals weren't returned,
        # so hand the issuer their stranded vessel back.
        if c.get("mission_type") == cdb.RESCUE:
            await _restore_issuer_vessel(gid, contract_id, c)
        log.info("KSP: %s paid fine for contract %s", user["username"], contract_id)
        return ContractAcceptResponse(success=True, message="Fine paid. Contract closed.")

    # ── Sue ── escalate to the moderator channel for review.
    if action == "sue":
        mod_ch_id = settings.CONTRACT_MOD_CHANNEL_ID
        if not mod_ch_id:
            return ContractAcceptResponse(success=False, message="Moderator review is not configured.")
        cdb.update_contract(gid, contract_id, status=cdb.MOD_REVIEW)
        c["status"] = cdb.MOD_REVIEW
        if _bot_instance:
            try:
                import discord
                from i18n import t
                from cogs.contract_views import ModReviewView, _embed
                ch = _bot_instance.get_channel(mod_ch_id) or await _bot_instance.fetch_channel(mod_ch_id)
                e = _embed(c, gid)
                e.title = f"⚖️ {t(gid, 'ct.mod_review')}"
                e.color = discord.Color.purple()
                # Why the submission was refused (AI verdict or issuer note), so mods
                # can judge whether the refusal was wrong.
                reason = c.get("review_reason")
                if reason:
                    e.add_field(name="Refusal Reason", value=str(reason)[:1024], inline=False)
                # Blueprint (.craft) + screenshots the player submitted.
                files = c.get("submitted_files", [])
                if files:
                    e.add_field(name="📁 Submitted Files", value="\n".join(
                        f"📎 [{f['filename']}]({f['url']})" for f in files), inline=False)
                await ch.send(embed=e, view=ModReviewView(contract_id, gid))
            except Exception as exc:
                log.warning("Could not post sue case to mod channel: %s", exc)
        log.info("KSP: %s sued contract %s", user["username"], contract_id)
        return ContractAcceptResponse(success=True, message="Case escalated to moderators.")

    # ── Settle ── ask the issuer to drop the contract with no exchange.
    if action == "settle":
        if is_bot_issued:
            return ContractAcceptResponse(success=False, message="AI contracts cannot be settled.")
        if _bot_instance:
            try:
                import discord
                from i18n import t
                from cogs.contract_views import SettleApprovalView
                issuer = await _bot_instance.fetch_user(issuer_id)
                e = discord.Embed(
                    title=f"🤝 {t(gid, 'ct.settle_request')}",
                    description=t(gid, 'ct.settle_desc', name=c['contractor_name']),
                    color=discord.Color.light_grey())
                await issuer.send(embed=e, view=SettleApprovalView(contract_id, gid))
            except Exception as exc:
                log.warning("Could not send settle request: %s", exc)
                return ContractAcceptResponse(success=False, message="Could not reach the issuer on Discord.")
        log.info("KSP: %s requested settlement for contract %s", user["username"], contract_id)
        return ContractAcceptResponse(success=True, message="Settlement request sent to the issuer.")

    # ── More Time ── extend the deadline (bot: auto; human: issuer approves).
    if action == "more_time":
        if is_bot_issued:
            tz = timezone(timedelta(hours=3))
            now = datetime.now(tz)
            days_to_sunday = 6 - now.weekday()
            if days_to_sunday <= 0:
                days_to_sunday = 7
            end_of_week = (now + timedelta(days=days_to_sunday)).strftime("%Y-%m-%d")
            cdb.update_contract(gid, contract_id, due_date=end_of_week, status=cdb.ACTIVE)
            log.info("KSP: %s auto-extended bot contract %s to %s",
                     user["username"], contract_id, end_of_week)
            return ContractAcceptResponse(
                success=True, message=f"Deadline extended to {end_of_week}. Submit again!")

        new_date = (req.new_date or "").strip()
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
        except ValueError:
            return ContractAcceptResponse(
                success=False, message="A valid new date (YYYY-MM-DD) is required.")
        if _bot_instance:
            try:
                import discord
                from i18n import t
                from cogs.contract_views import MoreTimeApprovalView
                issuer = await _bot_instance.fetch_user(issuer_id)
                e = discord.Embed(
                    title=f"⏰ {t(gid, 'ct.moretime_request')}",
                    description=t(gid, 'ct.moretime_desc', name=c['contractor_name'],
                                 old=c['due_date'], new=new_date),
                    color=discord.Color.blue())
                await issuer.send(embed=e, view=MoreTimeApprovalView(contract_id, gid, new_date))
            except Exception as exc:
                log.warning("Could not send more-time request: %s", exc)
                return ContractAcceptResponse(success=False, message="Could not reach the issuer on Discord.")
        log.info("KSP: %s requested more time (%s) for contract %s",
                 user["username"], new_date, contract_id)
        return ContractAcceptResponse(success=True, message="Time extension request sent to the issuer.")

    return ContractAcceptResponse(success=False, message=f"Unknown dispute action: {action}")


@app.post("/api/v1/contracts/{contract_id}/cancel", response_model=ContractAcceptResponse)
async def cancel_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Cancel a contract (available to issuer or contractor for pending/active contracts)."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        return ContractAcceptResponse(success=False, message="Contract not found.")

    # Only issuer or contractor can cancel
    if c.get("issuer_id") != uid and c.get("contractor_id") != uid:
        return ContractAcceptResponse(success=False, message="Not your contract.")

    # Only pending or active contracts can be cancelled
    if c.get("status") not in [cdb.PENDING, cdb.ACTIVE]:
        return ContractAcceptResponse(success=False, message=f"Cannot cancel a {c.get('status')} contract.")

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED)

    # Refund escrow to issuer
    issuer_id = int(c["issuer_id"])
    bot_uid = _get_bot_user_id()
    if issuer_id != bot_uid:
        await store.add_balance(gid, issuer_id, c["payment"])

    log.info("KSP: %s cancelled contract %s (refunded %d to issuer %s)",
             user["username"], contract_id, c["payment"], c["issuer_id"])

    # Notify the other party (not the one who cancelled) so their in-game contract
    # list updates live. Skip bot-issued counterparties.
    other_id = c["contractor_id"] if str(c.get("issuer_id")) == uid else c.get("issuer_id")
    if other_id and int(other_id) != bot_uid:
        _create_notification(
            gid, int(other_id), "contract_cancelled",
            "🚫 Contract Cancelled",
            f"{user['username']} cancelled \"{c['mission'][:80]}\".",
            data={"contract_id": contract_id},
        )

    # Rescue: the rescue won't happen — return the issuer's vessel to its spot.
    if c.get("mission_type") == cdb.RESCUE:
        await _restore_issuer_vessel(gid, contract_id, c)

    return ContractAcceptResponse(success=True, message="Contract cancelled. Escrow refunded.")


@app.post("/api/v1/contracts/{contract_id}/give_up", response_model=ContractAcceptResponse)
async def give_up_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Contractor gives up on an active contract they accepted.

    The proactive counterpart to the dispute 'pay_fine' action: the contractor pays
    the agreed fine to the issuer (who also gets their escrowed payment back) and the
    contract closes. Lets a contractor back out *before* submitting, at the cost of the
    penalty they agreed to. Only the contractor may give up, and only while the
    contract is active — pending uses Decline, submitted/disputed use the review and
    dispute flows. Refused if the contractor can't cover the fine.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        return ContractAcceptResponse(success=False, message="Contract not found.")

    if str(c.get("contractor_id")) != uid:
        return ContractAcceptResponse(success=False, message="Only the contractor can give up a contract.")

    if c.get("status") != cdb.ACTIVE:
        return ContractAcceptResponse(success=False, message=f"Cannot give up a {c.get('status')} contract.")

    issuer_id = int(c["issuer_id"])
    contractor_id = int(c["contractor_id"])
    bot_uid = _get_bot_user_id()
    fine = c.get("fine", 0)

    # The fine is the agreed penalty for backing out — charged regardless of who
    # issued the contract. Block the give-up if they can't cover it.
    bal = store.get_user(gid, contractor_id)["balance"]
    if bal < fine:
        return ContractAcceptResponse(
            success=False,
            message=f"You need {fine} {settings.CURRENCY_SYMBOL} to pay the fine and give up.")
    if fine:
        await store.add_balance(gid, contractor_id, -fine)

    # Release escrow (+ the fine) to the issuer. A bot issuer has no wallet to credit
    # (same gating as cancel), but the contractor still pays the penalty above.
    if issuer_id != bot_uid:
        await store.add_balance(gid, issuer_id, fine + c["payment"])

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED,
                        completed_at=datetime.utcnow().isoformat())

    log.info("KSP: %s gave up contract %s (fine %d to issuer %s)",
             user["username"], contract_id, fine, c["issuer_id"])

    if issuer_id != bot_uid:
        _create_notification(
            gid, issuer_id, "contract_cancelled", "🏳️ Contract Given Up",
            f"{c['contractor_name']} gave up on \"{c['mission'][:80]}\" and paid the "
            f"{fine} {settings.CURRENCY_SYMBOL} fine.",
            {"contract_id": contract_id},
        )

    # Rescue: the rescuer backed out — return the issuer's stranded vessel to its spot.
    if c.get("mission_type") == cdb.RESCUE:
        await _restore_issuer_vessel(gid, contract_id, c)

    msg = (f"Contract given up. You paid the {fine} {settings.CURRENCY_SYMBOL} fine."
           if fine else "Contract given up.")
    return ContractAcceptResponse(success=True, message=msg)

# ── Corporations ─────────────────────────────────────────────────────────────

@app.get("/api/v1/corps/list", response_model=CorpListResponse)
async def list_corps(user: dict = Depends(get_current_user)):
    """List all corporations in the guild."""
    gid = int(user["guild_id"])

    corps_col = _db.collection("guilds").document(str(gid)).collection("corps")
    corps = []
    for doc in corps_col.stream():
        d = doc.to_dict()
        if d:
            corps.append(CorpInfo(
                owner_id=doc.id,
                owner_name=d.get("owner_name", d.get("name", "Unknown")),
                corp_name=d.get("name", "Unknown Corp"),
            ))

    return CorpListResponse(corps=corps)


@app.post("/api/v1/contracts/create", response_model=ContractAcceptResponse)
async def create_contract_from_ksp(req: ContractCreateRequest, user: dict = Depends(get_current_user)):
    """Create a new contract from the KSP mod (issuer = current user, contractor = corp owner)."""
    from datetime import date
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    contractor_id = int(req.contractor_id)

    # Self-contract check
    if contractor_id == uid and not settings.CONTRACT_ALLOW_SELF:
        return ContractAcceptResponse(success=False, message="You can't contract yourself.")

    # Validate date
    try:
        dt = datetime.strptime(req.due_date, "%Y-%m-%d").date()
        if dt <= date.today():
            return ContractAcceptResponse(success=False, message="Due date must be in the future.")
    except ValueError:
        return ContractAcceptResponse(success=False, message="Invalid date format. Use YYYY-MM-DD.")

    # Check balance (need enough for escrow)
    u = store.get_user(gid, uid)
    bal = u.get("balance", 0)
    if bal < req.payment:
        return ContractAcceptResponse(
            success=False,
            message=f"Insufficient balance ({req.payment} needed, you have {bal}).",
        )

    # Check contract limit
    count = cdb.count_active(gid, uid)
    if count >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
        return ContractAcceptResponse(
            success=False,
            message=f"Active contract limit reached ({settings.MAX_ACTIVE_CONTRACTS_PER_USER}).",
        )

    # Resolve contractor name from corp data
    corp = _get_corp(gid, contractor_id)
    contractor_name = corp.get("owner_name", "Unknown") if corp else "Unknown"

    # Escrow: lock the payment
    await store.add_balance(gid, uid, -req.payment)

    # Create contract
    c = cdb.create_contract(
        guild_id=gid,
        issuer_id=uid,
        issuer_name=user["username"],
        contractor_id=contractor_id,
        contractor_name=contractor_name,
        mission=req.mission,
        payment=req.payment,
        fine=req.fine,
        due_date=req.due_date,
        modlist=req.modlist,
    )

    # Always let the AI read the mission text and decide the constraints (and
    # situation/body), even when the caller pins the contract type — otherwise
    # craft-build contracts, which are exactly the ones that carry part limits,
    # would never get AI-extracted limits. An explicit craft_build/active_vessel
    # then overrides only the *type* the AI guessed. flag_design isn't a vessel,
    # so it skips extraction entirely.
    ctype = (req.contract_type or "auto").lower()
    if ctype == "flag_design":
        cdb.update_contract(gid, c["contract_id"], mission_type=ctype)
    else:
        await _classify_single_contract(gid, c["contract_id"], req.mission)
        if ctype in ("craft_build", "active_vessel"):
            cdb.update_contract(gid, c["contract_id"], mission_type=ctype)

    # DM the contractor via Discord
    if _bot_instance:
        try:
            import discord
            from cogs.contract_views import ContractOfferView, _embed

            guild = _bot_instance.get_guild(gid)
            if guild:
                member = guild.get_member(contractor_id) or await guild.fetch_member(contractor_id)
                if member:
                    e = _embed(c, gid)
                    e.description = f"📜 You received a new contract offer from **{user['username']}** (via KSP)!"
                    view = ContractOfferView(c["contract_id"], gid)
                    dm_msg = await member.send(embed=e, view=view)
                    cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except Exception as exc:
            log.error("Failed to DM contractor %d: %s", contractor_id, exc)
            # Don't fail the contract creation — they'll see it in notifications

    # Also create a notification
    _create_notification(
        gid, contractor_id, "contract_incoming",
        "📜 New Contract Offer",
        f"{user['username']} sent you a contract: {req.mission[:100]}",
        {"contract_id": c["contract_id"]},
    )

    log.info("KSP: %s created contract %s for user %d (%d coins)",
             user["username"], c["contract_id"], contractor_id, req.payment)

    return ContractAcceptResponse(success=True, message=f"Contract sent! ID: {c['contract_id']}")


# Stock Kerbol-system bodies — used as a server-side fallback to flag a rescue
# target as "modded" when the client didn't send is_modded. (KNOWN_CELESTIAL_BODIES
# deliberately also lists popular modded bodies, so it can't be used for this.)
_STOCK_BODIES = {
    "kerbol", "sun", "moho", "eve", "gilly", "kerbin", "mun", "minmus",
    "duna", "ike", "dres", "jool", "laythe", "vall", "tylo", "bop", "pol", "eeloo",
}


@app.post("/api/v1/contracts/create_rescue", response_model=ContractAcceptResponse)
async def create_rescue_contract(
    contractor_id: str = Form(...),
    mission: str = Form(...),
    payment: int = Form(...),
    fine: int = Form(0),
    due_date: str = Form(...),
    modlist: Optional[str] = Form(None),
    body: str = Form(...),
    mode: str = Form("orbit"),
    ap: Optional[float] = Form(None),
    pe: Optional[float] = Form(None),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    margin_alt: float = Form(0.0),
    margin_pos: float = Form(0.0),
    is_modded: bool = Form(False),
    rescue_pid: Optional[str] = Form(None),
    kerbals: str = Form("[]"),         # JSON list of tagged names: ["{issuer}'s Jeb Kerman", ...]
    vessel_node: UploadFile = File(...),  # gzipped issuer vessel snapshot (the wreck)
    user: dict = Depends(get_current_user),
):
    """Create a rescue contract from the KSP mod.

    The issuer is in flight on a crewed vessel; their client snapshots that vessel
    (crew kept as-is — they're tagged "{issuer}'s {kerbal}" when the rescuer imports
    the wreck), captures the delivery target, and uploads it here. The wreck node is
    stored so the rescuer's client can spawn it on accept. The issuer's client removes
    its own copy of the vessel locally once this returns success.
    """
    import json

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    try:
        contractor_uid = int(contractor_id)
    except (TypeError, ValueError):
        return ContractAcceptResponse(success=False, message="Invalid contractor.")

    if contractor_uid == uid and not settings.CONTRACT_ALLOW_SELF:
        return ContractAcceptResponse(success=False, message="You can't contract yourself.")

    # Validate date
    from datetime import date
    try:
        dt = datetime.strptime(due_date, "%Y-%m-%d").date()
        if dt <= date.today():
            return ContractAcceptResponse(success=False, message="Due date must be in the future.")
    except ValueError:
        return ContractAcceptResponse(success=False, message="Invalid date format. Use YYYY-MM-DD.")

    # The tagged kerbal names the rescuer must recover ("{issuer}'s {kerbal}").
    try:
        rescue_kerbals = json.loads(kerbals) if kerbals else []
        if not isinstance(rescue_kerbals, list):
            rescue_kerbals = []
    except Exception:
        rescue_kerbals = []
    rescue_kerbals = [str(k) for k in rescue_kerbals if k]
    if not rescue_kerbals:
        return ContractAcceptResponse(success=False, message="No crew aboard to rescue.")

    # Balance + active-contract limit (same gates as a regular contract).
    u = store.get_user(gid, uid)
    if u.get("balance", 0) < payment:
        return ContractAcceptResponse(
            success=False, message=f"Insufficient balance ({payment} needed).")
    if cdb.count_active(gid, uid) >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
        return ContractAcceptResponse(
            success=False,
            message=f"Active contract limit reached ({settings.MAX_ACTIVE_CONTRACTS_PER_USER}).")

    from cogs.corps import _get_corp
    corp = _get_corp(gid, contractor_uid)
    contractor_name = corp.get("owner_name", "Unknown") if corp else "Unknown"

    if not is_modded and body.strip().lower() not in _STOCK_BODIES:
        is_modded = True

    rescue_target = {
        "body": body, "mode": (mode or "orbit").lower(),
        "ap": ap, "pe": pe, "lat": lat, "lon": lon,
        "margin_alt": margin_alt, "margin_pos": margin_pos, "is_modded": is_modded,
    }

    # Escrow the payment.
    await store.add_balance(gid, uid, -payment)

    c = cdb.create_contract(
        guild_id=gid, issuer_id=uid, issuer_name=user["username"],
        contractor_id=contractor_uid, contractor_name=contractor_name,
        mission=mission, payment=payment, fine=fine, due_date=due_date,
        modlist=modlist,
        mission_type=cdb.RESCUE,
        rescue_target=rescue_target,
        rescue_kerbals=rescue_kerbals,
        rescue_pid=rescue_pid,
    )

    # Store the wreck snapshot (gzipped ConfigNode) in Firebase Storage.
    try:
        node_bytes = await vessel_node.read()
        node_url = await cdb.upload_to_storage(
            c["contract_id"], "rescue_vessel.cfg", node_bytes, "application/gzip")
        cdb.update_contract(gid, c["contract_id"], rescue_vessel_node_url=node_url)
    except Exception as exc:
        # Roll the contract back — without the wreck node the rescue can't happen.
        log.error("Rescue vessel upload failed for %s: %s", c["contract_id"], exc)
        cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
        await store.add_balance(gid, uid, payment)
        return ContractAcceptResponse(success=False, message="Failed to store the rescue vessel.")

    # DM + notify the contractor, exactly like create_contract_from_ksp.
    if _bot_instance:
        try:
            import discord
            from cogs.contract_views import ContractOfferView, _embed
            guild = _bot_instance.get_guild(gid)
            if guild:
                member = guild.get_member(contractor_uid) or await guild.fetch_member(contractor_uid)
                if member:
                    e = _embed(c, gid)
                    e.description = (f"🛟 **{user['username']}** needs a rescue at **{body}** "
                                     f"({len(rescue_kerbals)} kerbal(s)) — via KSP!")
                    dm_msg = await member.send(embed=e, view=ContractOfferView(c["contract_id"], gid))
                    cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except Exception as exc:
            log.error("Failed to DM rescue contractor %d: %s", contractor_uid, exc)

    _create_notification(
        gid, contractor_uid, "contract_incoming",
        "🛟 New Rescue Mission",
        f"{user['username']} needs {len(rescue_kerbals)} kerbal(s) rescued at {body}.",
        {"contract_id": c["contract_id"]},
    )

    log.info("KSP: %s created RESCUE contract %s for user %d (%d coins, %d kerbals)",
             user["username"], c["contract_id"], contractor_uid, payment, len(rescue_kerbals))
    return ContractAcceptResponse(success=True, message=f"Rescue contract sent! ID: {c['contract_id']}")


def _extract_crew_names(vn_data: bytes | None) -> set[str]:
    """Pull assigned crew names out of a (gzipped) vessel ConfigNode. KSP stores
    assigned crew as `crew = <Name>` lines on PART nodes; rescued kerbals keep
    their renamed "{issuer}'s {kerbal} Kerman" names in the rescue craft."""
    import gzip
    import re
    if not vn_data:
        return set()
    try:
        text = gzip.decompress(vn_data).decode("utf-8", "ignore")
    except (OSError, EOFError):
        text = vn_data.decode("utf-8", "ignore")
    except Exception:
        return set()
    names: set[str] = set()
    for m in re.finditer(r"^\s*crew\s*=\s*(.+?)\s*$", text, re.MULTILINE):
        val = m.group(1).strip()
        if val and not val.isdigit():
            names.add(val)
    return names


def _validate_rescue_submission(c: dict, vessel_data: str | None, vn_data: bytes | None):
    """Defense-in-depth recheck of a rescue submission: right body + situation
    (from telemetry) and every stranded kerbal aboard (from the node). Returns
    (ok, reason). Orbit/surface margins are enforced authoritatively client-side."""
    rt = c.get("rescue_target") or {}
    body = (rt.get("body") or "").strip().lower()
    mode = (rt.get("mode") or "orbit").lower()

    vd: dict = {}
    if vessel_data:
        import json
        try:
            vd = json.loads(vessel_data)
        except Exception:
            vd = {}
    if vd:
        vbody = (vd.get("body") or "").strip().lower()
        if body and vbody and vbody != body:
            return False, f"Rescue craft is at {vd.get('body')}, must be at {rt.get('body')}."
        sit = (vd.get("situation") or "").upper()
        if mode == "orbit" and sit and sit != "ORBITING":
            return False, "Rescue craft must be ORBITING the target."
        if mode == "surface" and sit and sit not in ("LANDED", "SPLASHED"):
            return False, "Rescue craft must be LANDED/SPLASHED at the target."

    names = _extract_crew_names(vn_data)
    if names:
        missing = [k for k in c.get("rescue_kerbals", []) if k not in names]
        if missing:
            return False, f"Rescue craft is missing kerbals: {', '.join(missing)}."
    return True, ""


async def _deliver_rescue_craft(gid: int, contract_id: str, c: dict):
    """On approval: deliver the rescue craft (now carrying the kerbals home) to the
    issuer as a live-vessel import. Crew names are tagged/stripped by the issuer's
    client on import (their own kerbals strip back to original), so no server-side
    rename is needed. owner_name = the contractor who handed the craft over."""
    url = c.get("delivered_vessel_node_url") or c.get("vessel_node_url")
    if not url:
        log.warning("Rescue %s approved but has no delivered vessel node.", contract_id)
        return
    issuer_id = int(c["issuer_id"])
    craft_name = (c.get("vessel_data") or {}).get("vessel_name") or "Rescue Craft"
    imp.enqueue(gid, issuer_id, "rescue_delivery", contract_id, craft_name,
                vessel_node_url=url, owner_name=c.get("contractor_name", ""))
    # Credit the rescuer with a completed rescue for the leaderboard/stats.
    try:
        await store.add_rescue(gid, int(c["contractor_id"]))
    except Exception as exc:
        log.warning("Could not record rescue stat for contract %s: %s", contract_id, exc)
    _create_notification(
        gid, issuer_id, "rescue_delivered", "🛟 Kerbals Returned!",
        "Your rescued kerbals are home — the rescue craft will appear in your save.",
        {"contract_id": contract_id},
    )
    log.info("Rescue %s: delivered craft to issuer %d", contract_id, issuer_id)


async def _restore_issuer_vessel(gid: int, contract_id: str, c: dict):
    """On failure (cancel / rescuer paid fine / etc.): give the issuer their original
    vessel back at its original spot. The stored wreck node holds the original orbit
    and crew; owner_name = the issuer, so their client strips any tag back to the
    original kerbal names on import."""
    if not c.get("issuer_vessel_removed"):
        return  # never removed (e.g. failed before acceptance) → nothing to do
    url = c.get("rescue_vessel_node_url")
    if not url:
        log.warning("Rescue %s failed but has no stored wreck node to restore.", contract_id)
        return
    issuer_id = int(c["issuer_id"])
    imp.enqueue(gid, issuer_id, "rescue_delivery", contract_id, "Stranded Vessel",
                vessel_node_url=url, owner_name=c.get("issuer_name", ""))
    cdb.update_contract(gid, contract_id, issuer_vessel_removed=False)
    _create_notification(
        gid, issuer_id, "rescue_failed", "↩️ Rescue Cancelled",
        "The rescue didn't go through — your vessel is being returned to its place.",
        {"contract_id": contract_id},
    )
    log.info("Rescue %s: restored issuer %d vessel", contract_id, issuer_id)


# ── Submissions ──────────────────────────────────────────────────────────────

@app.post("/api/v1/contracts/{contract_id}/submit", response_model=SubmissionResult)
async def submit_contract(
    contract_id: str,
    craft_file: Optional[UploadFile] = File(None),
    vessel_node: Optional[UploadFile] = File(None),
    loadmeta: Optional[str] = Form(None),
    vessel_data: Optional[str] = Form(None),
    screenshot1: Optional[UploadFile] = File(None),
    screenshot2: Optional[UploadFile] = File(None),
    screenshot3: Optional[UploadFile] = File(None),
    # Multi-vessel submissions send one render per craft under this repeated field
    # (uncapped). The numbered fields above are kept for older clients.
    screenshots: List[UploadFile] = File(default=[]),
    modlist: Optional[str] = Form(None),
    used_modlist: Optional[str] = Form(None),
    # Per-part summary of the submitted craft (JSON array) used to verify the
    # contract's part-limit constraints. See data/mission_constraints.py.
    used_parts: Optional[str] = Form(None),
    # Craft's stock-calculated vacuum Δv (m/s) — the bot can't recompute it, so a
    # min/max-Δv mission limit is verified against this client-reported value.
    delta_v_vac: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
):
    """
    Submit a contract completion with craft file, loadmeta, vessel data, and screenshots.
    Files are uploaded to Firebase Storage. AI review is triggered for bot-issued contracts.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    bot_uid = _get_bot_user_id()

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if c.get("contractor_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your contract")

    if c.get("status") != cdb.ACTIVE:
        return SubmissionResult(success=False, message="Contract is not active.")

    # Read the vessel node once (used by both the rescue check below and the
    # Storage upload further down). UploadFile.read() can only be consumed once.
    vn_data = await vessel_node.read() if vessel_node else None

    # Rescue: server-side defense-in-depth before accepting the submission — the
    # rescue craft must be at the target body/situation and carry every stranded
    # kerbal. The client gates this too, but a modified DLL must not bypass it.
    if c.get("mission_type") == cdb.RESCUE:
        ok, reason = _validate_rescue_submission(c, vessel_data, vn_data)
        if not ok:
            return SubmissionResult(success=False, message=reason)

    # Server-side part-restriction check (defense-in-depth — the client also gates this,
    # but an old/modified DLL must not bypass it). The contract's modlist is an allow-list
    # of top-level mod folders (tokens prefixed with "-" are client-only exclude paths we
    # can't evaluate at folder granularity, so they're ignored here). used_modlist is the
    # set of folders the submitted craft actually uses. Skipped if either side is absent.
    required_modlist = c.get("modlist")
    if required_modlist and used_modlist:
        allowed = {
            tok.strip().lower()
            for tok in required_modlist.split(",")
            if tok.strip() and not tok.strip().startswith("-")
        }
        if allowed:
            illegal = sorted(
                f.strip()
                for f in used_modlist.split(",")
                if f.strip() and f.strip().lower() not in allowed
            )
            if illegal:
                log.info("Submission rejected for contract %s: craft uses disallowed mods %s",
                         contract_id, illegal)
                return SubmissionResult(
                    success=False,
                    message=f"Craft uses parts outside this contract's allowed mods: {', '.join(illegal)}.",
                )

    # Server-side part-limit ("mission limit") check — authoritative re-check of
    # the constraints the editor/submit gate already enforce client-side. Skipped
    # when the contract has no constraints or the client reported no part summary.
    constraints = c.get("constraints")
    if not mc.is_empty(constraints) and (used_parts or delta_v_vac):
        import json
        # Resolve loose part mentions against the submitter's catalog so the
        # authoritative check matches the exact part, with loose fallback.
        constraints = _resolve_constraints(constraints, gid, uid, c.get("mission", ""))
        try:
            parsed_parts = json.loads(used_parts) if used_parts else []
        except Exception as exc:
            log.warning("Bad used_parts payload for contract %s: %s", contract_id, exc)
            parsed_parts = None
        # Client-reported vacuum Δv (None if absent/unparseable => Δv limit skipped).
        try:
            dv = float(delta_v_vac) if delta_v_vac not in (None, "") else None
        except (TypeError, ValueError):
            dv = None
        if isinstance(parsed_parts, list):
            violations = mc.verify_used_parts(constraints, parsed_parts, delta_v=dv)
            if violations:
                log.info("Submission rejected for contract %s: constraint violations %s",
                         contract_id, violations)
                return SubmissionResult(
                    success=False,
                    message="Craft breaks this contract's mission limits:\n- " + "\n- ".join(violations),
                )

    # Upload files to Firebase Storage
    stored_files = []

    # Craft file
    if craft_file:
        data = await craft_file.read()
        try:
            url = await cdb.upload_to_storage(
                contract_id, craft_file.filename, data,
                craft_file.content_type or "application/octet-stream"
            )
            stored_files.append({"filename": craft_file.filename, "url": url,
                                 "content_type": craft_file.content_type or "application/octet-stream"})
        except Exception as exc:
            log.error("Craft upload failed: %s", exc)

    # Screenshots — legacy numbered fields plus the uncapped repeated field, so a
    # multi-craft submission stores a render for every selected vessel.
    all_screenshots = [s for s in (screenshot1, screenshot2, screenshot3) if s]
    all_screenshots += [s for s in (screenshots or []) if s]
    for ss in all_screenshots:
        data = await ss.read()
        try:
            url = await cdb.upload_to_storage(
                contract_id, ss.filename, data,
                ss.content_type or "image/png"
            )
            stored_files.append({"filename": ss.filename, "url": url,
                                 "content_type": ss.content_type or "image/png"})
        except Exception as exc:
            log.error("Screenshot upload failed: %s", exc)

    if not stored_files:
        return SubmissionResult(success=False, message="No files uploaded successfully.")

    has_image = any(f["content_type"].startswith("image/") for f in stored_files)
    if not has_image:
        return SubmissionResult(success=False, message="At least one screenshot is required.")

    # Update contract status
    now = datetime.utcnow().isoformat()
    update_fields = {
        "status": cdb.SUBMITTED,
        "submitted_files": stored_files,
        "submitted_at": now,
        "contractor_modlist": modlist,
    }

    # Store vessel data and loadmeta if provided
    parsed_vessel_data: dict | None = None
    if vessel_data:
        import json
        try:
            parsed_vessel_data = json.loads(vessel_data)
            update_fields["vessel_data"] = parsed_vessel_data
        except Exception:
            update_fields["vessel_data_raw"] = vessel_data

    if loadmeta:
        update_fields["loadmeta"] = loadmeta

    # Orbital telemetry diagram — rendered once from the captured vessel state and
    # persisted to Storage so it can be surfaced both in the Discord review embed
    # and the in-game review window. Only present when the client sent vessel data
    # (active-vessel / rescue submissions); other submission types skip it.
    if parsed_vessel_data:
        try:
            from orbit_render import render_orbit

            # One orbit diagram per submitted craft: the active (contract) vessel
            # first, then any extras sent in a multi-vessel submission.
            snaps = []
            active_snap = parsed_vessel_data.get("active_vessel") or parsed_vessel_data
            if isinstance(active_snap, dict):
                snaps.append(active_snap)
            for sv in (parsed_vessel_data.get("sent_vessels") or []):
                if isinstance(sv, dict):
                    snaps.append(sv)

            telemetry_urls = []
            for idx, snap in enumerate(snaps):
                try:
                    orbit_png = render_orbit(snap)
                except Exception as exc:
                    log.warning("orbit render failed for vessel %d on %s: %s", idx, contract_id, exc)
                    continue
                if not orbit_png:
                    continue

                fname = "orbit_telemetry.png" if idx == 0 else f"orbit_telemetry_{idx}.png"
                url = await cdb.upload_to_storage(contract_id, fname, orbit_png, "image/png")
                telemetry_urls.append(url)

            # Telemetry diagrams stay OUT of the blueprint image list — they're surfaced
            # in the dedicated in-game telemetry window (one per craft) and the Discord
            # embed. telemetry_image_url keeps the active craft's diagram for back-compat.
            if telemetry_urls:
                update_fields["telemetry_image_url"] = telemetry_urls[0]
                update_fields["telemetry_image_urls"] = telemetry_urls
        except Exception as exc:
            log.warning("Failed to render/store orbit telemetry for %s: %s", contract_id, exc)

    # Upload vessel node (full vessel state for transfer) to Storage
    if vn_data is not None:
        try:
            vn_url = await cdb.upload_to_storage(
                contract_id, "vessel_node.cfg", vn_data, "application/gzip"
            )
            update_fields["vessel_node_url"] = vn_url
            # For rescue, this is the craft that carries the kerbals home — delivered
            # to the issuer (with restored names) once they approve.
            if c.get("mission_type") == cdb.RESCUE:
                update_fields["delivered_vessel_node_url"] = vn_url
            log.info("Vessel node uploaded: %d bytes (gzipped)", len(vn_data))
        except Exception as exc:
            log.error("Vessel node upload failed: %s", exc)

    cdb.update_contract(gid, contract_id, **update_fields)

    # AI Review for bot-issued contracts
    is_bot_issued = str(c.get("issuer_id")) == str(bot_uid)

    if is_bot_issued:
        result = await _ai_review_submission(gid, uid, contract_id, c, stored_files, vessel_data, loadmeta)
        return result

    # Human-issued: notify issuer via Discord notification system AND Discord channel
    _create_notification(
        gid, int(c["issuer_id"]), "submission_received",
        "Contract Submission",
        f"{user['username']} submitted work for: {c['mission'][:50]}",
        {"contract_id": contract_id},
    )

    # Also post to the issuer's corp channel in Discord
    await _discord_notify_issuer(
        gid, int(c["issuer_id"]), contract_id, c, user["username"], stored_files,
        parsed_vessel_data,
    )

    log.info("KSP: %s submitted contract %s (human-issued)", user["username"], contract_id)
    return SubmissionResult(
        success=True,
        message="Submitted! Waiting for issuer review.",
        review_status="pending",
    )


async def _ai_review_submission(
    gid: int, uid: int, contract_id: str, c: dict,
    stored_files: list[dict], vessel_data: str | None, loadmeta: str | None,
) -> SubmissionResult:
    """Run Gemini AI review on a bot-issued contract submission."""
    import json
    from cogs.screenshots import _client as gemini_client, _MODEL

    if not gemini_client:
        # No AI configured — auto-accept
        return await _auto_accept_contract(gid, uid, contract_id, c)

    # Download screenshots for AI
    screenshots = [f for f in stored_files if f.get("content_type", "").startswith("image/")]
    if not screenshots:
        return SubmissionResult(success=False, message="No screenshots for AI review.")

    img_bytes_list = []
    for s in screenshots:
        try:
            raw = await cdb.download_url(s["url"])
            img_bytes_list.append(raw)
        except Exception:
            pass

    if not img_bytes_list:
        return SubmissionResult(success=False, message="Could not download screenshots for review.")

    # Build prompt including loadmeta context
    mission_desc = c.get("mission", "")
    extra_context = ""
    if loadmeta:
        extra_context += f"\n\nCraft loadmeta data:\n{loadmeta}"
    if vessel_data:
        extra_context += f"\n\nVessel telemetry data:\n{vessel_data}"

    review_prompt = (
        f"You are reviewing a KSP contract submission from the in-game mod client.\n"
        f"The mission was: \"{mission_desc}\"\n"
        f"{extra_context}\n\n"
        f"Analyze the screenshot(s) and any provided telemetry/loadmeta data.\n"
        f"Determine if the mission was completed successfully.\n"
        f"Additionally, assign the highest applicable KSP achievement level (1-15):\n"
        f"1. Kerbin Orbit | 2. Mun Landing | 3. Docking | 4. Duna Landing | 5. RSS Earth Orbit\n"
        f"6. Eve Landing | 7. Asteroid Redirect | 8. RSS Moon Landing | 9. Jool 5 | 10. Interstellar\n"
        f"11. RSS Mars | 12. RSS Venus Landing | 13. RSS Gas Giant | 14. Kerbol Grand Tour | 15. RSS Interstellar\n"
        f"If none clearly apply, set ksp_level to 0.\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{\n  "approved": true/false,\n  "reason": "brief explanation",\n  "ksp_level": integer\n}}'
    )

    from google.genai import types

    parts = [types.Part.from_text(text=review_prompt)]
    for img in img_bytes_list:
        parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))

    try:
        response = gemini_client.models.generate_content(
            model=_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=512),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        result = json.loads(raw.strip())
    except Exception as exc:
        log.error("AI review failed for KSP submission: %s", exc)
        return await _auto_accept_contract(gid, uid, contract_id, c)

    if result.get("approved", False):
        return await _auto_accept_contract(
            gid, uid, contract_id, c,
            result.get("reason", ""), result.get("ksp_level", 0)
        )
    else:
        reason = result.get("reason", "AI review did not approve this submission.")
        # Persist the reason so it can be shown to mods if the player sues.
        cdb.update_contract(gid, contract_id, status=cdb.DISPUTED, review_reason=reason)
        _create_notification(gid, uid, "review_result",
                             "❌ Submission Refused",
                             reason,
                             {"contract_id": contract_id})
        return SubmissionResult(
            success=True, message="Submission reviewed.",
            review_status="refused", reason=reason,
        )


async def _auto_accept_contract(
    gid: int, uid: int, contract_id: str, c: dict,
    reason: str = "", ksp_level: int = 0,
) -> SubmissionResult:
    """Accept a contract, grant rewards."""
    now = datetime.utcnow().isoformat()
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED, completed_at=now)

    # Grant payment
    await store.add_balance(gid, uid, c["payment"])

    # Grant XP
    diff = c["payment"] // settings.WEEKLY_COINS_PER_DIFFICULTY if settings.WEEKLY_COINS_PER_DIFFICULTY else 0
    xp = diff * settings.WEEKLY_XP_PER_DIFFICULTY
    if xp > 0:
        u = store.get_user(gid, uid)
        await store.set_xp(gid, uid, u["xp"] + xp)

    # KSP level award
    if ksp_level > 0:
        await store.add_unlocked_level(gid, uid, ksp_level)

    _create_notification(gid, uid, "review_result",
                         "✅ Mission Approved!",
                         f"{reason}\n+{c['payment']} KCoins, +{xp} XP" if reason else f"+{c['payment']} KCoins, +{xp} XP",
                         {"contract_id": contract_id, "ksp_level": ksp_level})

    log.info("KSP: Auto-accepted contract %s for user %d (+%d coins, +%d XP, level %d)",
             contract_id, uid, c["payment"], xp, ksp_level)

    # The finished craft goes to the builder's corporation channel.
    await _deliver_craft_to_corp(gid, uid, contract_id)

    return SubmissionResult(
        success=True, message="Mission approved!",
        review_status="approved", reason=reason,
        xp_awarded=xp, coins_awarded=c["payment"],
    )


# ── Notifications ────────────────────────────────────────────────────────────

def _notifications_col(guild_id: int, user_id: int):
    return (_db.collection("guilds").document(str(guild_id))
            .collection("ksp_notifications").document(str(user_id))
            .collection("items"))


def _create_notification(
    guild_id: int, user_id: int, notif_type: str,
    title: str, message: str, data: dict | None = None,
):
    """Create a notification in Firestore for a user and push it to any live
    WebSocket connections."""
    doc_id = uuid.uuid4().hex[:12]
    payload = {
        "id": doc_id,
        "type": notif_type,
        "title": title,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "data": data or {},
    }
    _notifications_col(guild_id, user_id).document(doc_id).set(payload)
    _push_notification(guild_id, user_id, payload)


@app.websocket("/ws/v1/notifications")
async def notifications_ws(websocket: WebSocket):
    """Live notification stream. The KSP client connects with the session token
    in the query string (UnityWebRequest cannot set headers on a WS handshake)."""
    token = websocket.query_params.get("token", "")
    user = verify_session_token(token, _get_api_secret()) if token else None
    if user is None:
        await websocket.close(code=1008)  # policy violation
        return

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    await _hub.connect(gid, uid, websocket)
    try:
        # Keep the socket open; client may send keepalive pings we just discard.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("WS: receive loop ended for user %d: %s", uid, exc)
    finally:
        _hub.disconnect(gid, uid, websocket)


@app.get("/api/v1/user/notifications", response_model=NotificationsResponse)
async def get_notifications(user: dict = Depends(get_current_user)):
    """Get recent notifications (read + unread) for the current user, newest first."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    col = _notifications_col(gid, uid)
    notifs = []

    # Single-field order_by is auto-indexed — no composite index needed.
    for doc in col.order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).limit(50).stream():
        notifs.append(Notification(**doc.to_dict()))

    return NotificationsResponse(
        notifications=notifs,
        unread_count=sum(1 for n in notifs if not n.read),
    )


@app.post("/api/v1/user/notifications/mark_read")
async def mark_notifications_read(user: dict = Depends(get_current_user)):
    """Mark all notifications as read."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    col = _notifications_col(gid, uid)
    for doc in col.where("read", "==", False).stream():
        doc.reference.update({"read": True})

    return {"success": True}


@app.post("/api/v1/user/notifications/{notif_id}/mark_read")
async def mark_notification_read(notif_id: str, user: dict = Depends(get_current_user)):
    """Mark a single notification as read."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    _notifications_col(gid, uid).document(notif_id).update({"read": True})
    return {"success": True}


@app.delete("/api/v1/user/notifications/{notif_id}")
async def dismiss_notification(notif_id: str, user: dict = Depends(get_current_user)):
    """Dismiss (delete) a single notification."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    _notifications_col(gid, uid).document(notif_id).delete()
    return {"success": True}


# ── Craft Download ───────────────────────────────────────────────────────────

@app.get("/api/v1/craft/download/{contract_id}")
async def download_craft(contract_id: str, user: dict = Depends(get_current_user)):
    """Get craft file download URL from a completed contract.

    Player-to-player contract crafts stay private to the two parties (issuer +
    contractor), who import them in the mod as usual. Bot-contract crafts are NOT
    served here — they're delivered to the builder's corp channel instead.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if c.get("status") != cdb.COMPLETED:
        raise HTTPException(status_code=400, detail="Contract not completed yet")

    is_bot_issued = str(c.get("issuer_id")) == str(_get_bot_user_id())
    if is_bot_issued:
        # Bot-contract crafts are delivered to the builder's corp channel, never
        # imported — this also blocks re-importing the live vessel into a save.
        raise HTTPException(
            status_code=403,
            detail="Bot-contract crafts are delivered to your corporation channel, not imported.",
        )
    if uid not in (str(c.get("issuer_id")), str(c.get("contractor_id"))):
        raise HTTPException(status_code=403, detail="This craft is private to the contract parties.")

    files = c.get("submitted_files", [])
    craft_files = [f for f in files if f.get("filename", "").endswith(".craft")]
    vessel_node_url = c.get("vessel_node_url")

    if not craft_files and not vessel_node_url:
        raise HTTPException(status_code=404, detail="No craft file or vessel data in submission")

    return {
        "craft_files": craft_files,
        "loadmeta": c.get("loadmeta"),
        "vessel_node_url": vessel_node_url,
    }


@app.get("/api/v1/contracts/{contract_id}/submission")
async def get_submission_preview(contract_id: str, user: dict = Depends(get_current_user)):
    """Return the contractor's submitted images (vessel render / blueprint) so the
    issuer can preview the work in-game before approving or refusing it.

    Restricted to the contract's two parties. Only the image files are returned —
    the craft file itself stays gated behind the existing /craft endpoint, which
    only opens up once the contract is completed.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if uid not in (str(c.get("issuer_id")), str(c.get("contractor_id"))):
        raise HTTPException(status_code=403, detail="This submission is private to the contract parties.")

    # Flag-design: watermarked preview while pending review; the clean full-res
    # flag is only exposed once the contract is completed (i.e. paid for).
    if c.get("mission_type") == cdb.FLAG_DESIGN:
        if c.get("status") == cdb.COMPLETED and c.get("flag_fullres_url"):
            url, fname = c["flag_fullres_url"], (c.get("flag_filename") or "flag.png")
        else:
            url, fname = c.get("flag_preview_url"), "flag_preview.png"
        images = [{"filename": fname, "url": url}] if url else []
        return {"images": images, "vessel_name": "", "telemetry_url": ""}

    images = [
        {"filename": f.get("filename"), "url": f.get("url")}
        for f in c.get("submitted_files", [])
        if (f.get("content_type") or "").startswith("image/") and f.get("url")
    ]

    vessel_name = (c.get("vessel_data") or {}).get("vessel_name") or ""
    # The orbital telemetry diagrams are stored separately (not in submitted_files) so
    # the mod shows them in their own window rather than mixed with blueprints. A
    # multi-craft submission has one per craft; telemetry_url stays for old clients.
    telemetry_urls = c.get("telemetry_image_urls") or (
        [c["telemetry_image_url"]] if c.get("telemetry_image_url") else []
    )
    telemetry_url = telemetry_urls[0] if telemetry_urls else ""
    return {
        "images": images,
        "vessel_name": vessel_name,
        "telemetry_url": telemetry_url,
        "telemetry_urls": telemetry_urls,
    }


@app.get("/api/v1/craft/imports/pending")
async def craft_imports_pending(user: dict = Depends(get_current_user)):
    """Crafts the player queued (in Discord) for auto-import into their save.

    The mod polls this at the Space Center, imports each entry, then acks it via
    POST /api/v1/craft/imports/{import_id}/done so it isn't imported twice.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    imports = []
    for e in imp.list_pending(gid, uid):
        # dedup_key lets the mod skip an entry it already processed into this save.
        dedup = e["ref_id"] if e.get("source") == "contract" else f"{e.get('source')}:{e['ref_id']}"
        imports.append({**e, "dedup_key": dedup})

    imports.sort(key=lambda x: x.get("created_at") or "")
    return {"imports": imports}


@app.post("/api/v1/craft/imports/{import_id}/done")
async def craft_import_done(import_id: str, user: dict = Depends(get_current_user)):
    """Ack a completed import — removes it from the player's queue."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    deleted = imp.delete(gid, uid, import_id)
    return {"success": deleted}


@app.post("/api/v1/craft/send")
async def craft_send_to_friend(
    file: UploadFile = File(...),
    recipient_id: str = Form(...),
    kind: str = Form("craft"),
    craft_name: str = Form("Craft"),
    user: dict = Depends(get_current_user),
):
    """Quicksend a craft/vessel from the KSP mod's Tools tab to another player.

    kind="vessel" delivers a LIVE vessel (the recipient's client spawns it in their
    save); kind="craft" delivers a .craft blueprint into their Ships folder. Both
    ride the per-user craft-import queue the mod already polls, so the recipient
    receives it automatically the next time they're at the Space Center. The payload
    arrives gzip-compressed (like submissions/listings); we store it decompressed.
    """
    import gzip
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    try:
        rid = int(recipient_id)
    except (TypeError, ValueError):
        return {"success": False, "message": "Invalid recipient."}

    if rid == uid:
        return {"success": False, "message": "You can't send a craft to yourself."}

    # Resolve the recipient — they must be a known player (have a corp, as the
    # in-game friend picker lists) or a current member of the guild.
    corp = _get_corp(gid, rid)
    recipient_name = corp.get("owner_name") if corp else None
    if recipient_name is None and _bot_instance:
        guild = _bot_instance.get_guild(gid)
        member = guild.get_member(rid) if guild else None
        if member:
            recipient_name = member.display_name
    if recipient_name is None:
        return {"success": False, "message": "That player isn't in this server."}

    kind = (kind or "craft").lower()
    if kind not in ("craft", "vessel"):
        return {"success": False, "message": "Unknown send type."}

    raw = await file.read()
    try:
        payload = gzip.decompress(raw)
    except (OSError, EOFError):
        payload = raw  # fall back if it wasn't compressed

    iid = uuid.uuid4().hex[:12]
    if kind == "vessel":
        filename = "vessel.cfg"
    else:
        filename = file.filename or f"{craft_name}.craft"
        if not filename.lower().endswith(".craft"):
            filename += ".craft"

    try:
        url = await asyncio.to_thread(imp.upload_gift, iid, filename, payload)
    except Exception as exc:
        log.error("Quicksend upload failed: %s", exc)
        return {"success": False, "message": "Failed to upload the craft."}

    if kind == "vessel":
        imp.enqueue(
            gid, rid, source="gift_vessel", ref_id=iid, craft_name=craft_name,
            vessel_node_url=url, owner_name=user["username"],
        )
        kind_label = "a live vessel"
    else:
        imp.enqueue(
            gid, rid, source="gift_craft", ref_id=iid, craft_name=craft_name,
            craft_url=url, craft_filename=filename, owner_name=user["username"],
        )
        kind_label = "a craft"

    _create_notification(
        gid, rid, "craft_gift",
        "🎁 Craft Received",
        f"{user['username']} sent you {kind_label}: {craft_name}. "
        f"Visit the Space Center in KSP to receive it.",
        {"craft_name": craft_name},
    )

    log.info("KSP: %s quicksent %s '%s' to %d", user["username"], kind, craft_name, rid)
    return {"success": True, "message": f"Sent to {recipient_name}!"}


# ── Marketplace ────────────────────────────────────────────────────────────────

@app.post("/api/v1/marketplace/list", response_model=MarketplaceListResult)
async def marketplace_list_craft(
    craft_file: UploadFile = File(...),
    blueprint: Optional[UploadFile] = File(None),
    craft_name: str = Form(...),
    craft_type: str = Form("VAB"),
    part_count: int = Form(0),
    mass: float = Form(0.0),
    cost: float = Form(0.0),
    price: int = Form(...),
    user: dict = Depends(get_current_user),
):
    """List a craft (.craft blueprint) for sale on the marketplace.

    The mod uploads the craft gzip-compressed (like contract submissions). We
    decompress and store the raw .craft so the buyer's DM delivery is a straight
    download. The listing is then posted to the marketplace Discord channel.
    """
    import gzip

    if not settings.MARKETPLACE_CHANNEL_ID:
        return MarketplaceListResult(success=False, message="The marketplace is not available right now.")

    if price < settings.MARKETPLACE_MIN_PRICE or price > settings.MARKETPLACE_MAX_PRICE:
        return MarketplaceListResult(
            success=False,
            message=f"Price must be between {settings.MARKETPLACE_MIN_PRICE} and "
                    f"{settings.MARKETPLACE_MAX_PRICE} KCoins.",
        )

    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    raw = await craft_file.read()
    # The mod gzips the craft; fall back to raw bytes if it wasn't compressed.
    try:
        craft_bytes = gzip.decompress(raw)
    except (OSError, EOFError):
        craft_bytes = raw

    filename = craft_file.filename or "craft.craft"
    if not filename.lower().endswith(".craft"):
        filename += ".craft"

    listing = mkt.create_listing(
        gid, uid, user["username"],
        craft_name=craft_name, craft_type=craft_type, part_count=part_count,
        mass=mass, cost=cost, price=price,
        craft_url="", craft_filename=filename,
    )

    try:
        url = await mkt.upload_craft(listing["listing_id"], filename, craft_bytes)
    except Exception as exc:
        log.error("Marketplace craft upload failed: %s", exc)
        return MarketplaceListResult(success=False, message="Failed to upload craft file.")

    mkt.update_listing(gid, listing["listing_id"], craft_url=url)
    listing["craft_url"] = url

    # Rendered blueprint image — shown publicly on the listing. Optional: if the
    # render failed client-side, the listing still posts without an image.
    if blueprint is not None:
        try:
            bp_data = await blueprint.read()
            bp_url = await mkt.upload_blueprint(
                listing["listing_id"], bp_data, blueprint.content_type or "image/png"
            )
            mkt.update_listing(gid, listing["listing_id"], blueprint_url=bp_url)
            listing["blueprint_url"] = bp_url
        except Exception as exc:
            log.error("Marketplace blueprint upload failed: %s", exc)

    # Post the listing to the Discord marketplace channel (runs on the bot loop).
    try:
        from cogs.marketplace import post_listing
        await post_listing(_bot_instance, gid, listing)
    except Exception as exc:
        log.error("Failed to post marketplace listing %s: %s", listing["listing_id"], exc)

    log.info("KSP: %s listed craft '%s' for %d (listing %s)",
             user["username"], craft_name, price, listing["listing_id"])
    return MarketplaceListResult(
        success=True,
        message="Your craft is now for sale!",
        listing_id=listing["listing_id"],
    )


@app.get("/api/v1/marketplace/listings", response_model=MarketplaceListingsResponse)
async def marketplace_listings(user: dict = Depends(get_current_user)):
    """Return all active marketplace listings."""
    gid = int(user["guild_id"])
    listings = [
        MarketplaceListing(
            listing_id=l["listing_id"],
            seller_id=l["seller_id"],
            seller_name=l.get("seller_name", ""),
            craft_name=l.get("craft_name", ""),
            craft_type=l.get("craft_type", ""),
            part_count=l.get("part_count", 0),
            mass=l.get("mass", 0.0),
            cost=l.get("cost", 0.0),
            price=l.get("price", 0),
            sales_count=l.get("sales_count", 0),
            created_at=l.get("created_at"),
        )
        for l in mkt.list_active(gid)
    ]
    return MarketplaceListingsResponse(listings=listings)


@app.post("/api/v1/marketplace/{listing_id}/delist", response_model=MarketplaceListResult)
async def marketplace_delist(listing_id: str, user: dict = Depends(get_current_user)):
    """Delist a craft the caller owns."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your listing")

    if listing.get("status") == mkt.ACTIVE:
        mkt.update_listing(gid, listing_id, status=mkt.DELISTED)
        listing["status"] = mkt.DELISTED

    # Disable the channel message buttons if we can find it.
    msg_id = listing.get("channel_msg_id")
    if msg_id and settings.MARKETPLACE_CHANNEL_ID and _bot_instance is not None:
        try:
            from cogs.marketplace import listing_embed
            channel = _bot_instance.get_channel(settings.MARKETPLACE_CHANNEL_ID)
            if channel:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=listing_embed(listing), view=None)
        except Exception as exc:
            log.warning("Could not update delisted message for %s: %s", listing_id, exc)

    return MarketplaceListResult(success=True, message="Craft delisted.", listing_id=listing_id)


# ── Checkpoint Hero Shots ─────────────────────────────────────────────────────

# Human-readable titles per checkpoint kind for the Discord post.
_CHECKPOINT_TITLES = {
    "rendezvous": "🤝 Rendezvous",
    "flyby": "🛰️ Flyby",
    "asteroid": "☄️ Asteroid encounter",
    "comet": "☄️ Comet encounter",
}


@app.post("/api/v1/checkpoint-photo", response_model=SubmissionResult)
async def checkpoint_photo(
    photo: UploadFile = File(...),
    kind: str = Form("checkpoint"),
    vessel_name: str = Form(""),
    body: str = Form(""),
    target_name: str = Form(""),
    caption: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Receive a milestone hero shot from the KSP mod and post it to the
    checkpoint-photos Discord channel.

    The image is sent straight to Discord as an attachment (no Firebase Storage)
    since these are ephemeral community posts, not durable submission records.
    """
    if not settings.CHECKPOINT_PHOTOS_ENABLED:
        return SubmissionResult(success=False, message="Checkpoint photos are disabled on this server.")

    if not settings.CHECKPOINT_PHOTOS_CHANNEL_ID:
        return SubmissionResult(success=False, message="Checkpoint photos are not enabled on this server.")

    if _bot_instance is None:
        return SubmissionResult(success=False, message="Bot is not ready.")

    channel = _bot_instance.get_channel(settings.CHECKPOINT_PHOTOS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await _bot_instance.fetch_channel(settings.CHECKPOINT_PHOTOS_CHANNEL_ID)
        except Exception as exc:
            log.error("Checkpoint channel %s unavailable: %s",
                      settings.CHECKPOINT_PHOTOS_CHANNEL_ID, exc)
            return SubmissionResult(success=False, message="The checkpoint photo channel is unavailable.")

    data = await photo.read()
    if not data:
        return SubmissionResult(success=False, message="Empty image.")

    import discord

    uid = int(user["user_id"])
    username = user.get("username") or "Kerbonaut"
    title = _CHECKPOINT_TITLES.get((kind or "").lower(), "📸 Mission milestone")

    lines = []
    if vessel_name:
        lines.append(f"**Vessel:** {vessel_name}")
    if body:
        lines.append(f"**Location:** {body}")
    if target_name:
        lines.append(f"**Subject:** {target_name}")
    if caption:
        lines.append(caption)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else None,
        color=0x2ECC71,
        timestamp=datetime.now(timezone.utc),
    )

    # Attribute the shot to the uploader, with their avatar when resolvable.
    author_icon = None
    discord_user = _bot_instance.get_user(uid)
    if discord_user is None:
        try:
            discord_user = await _bot_instance.fetch_user(uid)
        except Exception:
            discord_user = None
    if discord_user is not None:
        author_icon = discord_user.display_avatar.url
    embed.set_author(name=username, icon_url=author_icon)

    filename = "checkpoint.png"
    embed.set_image(url=f"attachment://{filename}")

    try:
        file = discord.File(io.BytesIO(data), filename=filename)
        await channel.send(embed=embed, file=file)
    except Exception as exc:
        log.error("Failed to post checkpoint photo for user %s: %s", uid, exc)
        return SubmissionResult(success=False, message="Failed to post the photo.")

    log.info("KSP: %s posted a %s checkpoint photo (vessel '%s')", username, kind, vessel_name)
    return SubmissionResult(success=True, message="Photo shared!")


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Bot Instance & User ID Helper ────────────────────────────────────────────

_bot_user_id: int = 0
_bot_instance = None  # discord.ext.commands.Bot reference

def set_bot_user_id(uid: int):
    global _bot_user_id
    _bot_user_id = uid

def set_bot_instance(bot):
    global _bot_instance
    _bot_instance = bot

def _get_bot_user_id() -> int:
    return _bot_user_id


async def _deliver_craft_to_corp(gid: int, builder_id: int, contract_id: str):
    """Post a completed bot-contract craft to the builder's corporation channel.

    Bot-contract deliverables go to the player's corp (as a downloadable .craft
    blueprint), not into anyone's save — so the corp shares the work and there's
    no live-vessel re-import. No-op if the player has no corp or the contract has
    no craft file (e.g. a flight-only mission).
    """
    if _bot_instance is None:
        return
    try:
        import discord
        from cogs.corps import find_user_corp

        c = cdb.get_contract(gid, contract_id)
        if not c:
            return
        craft_files = [f for f in c.get("submitted_files", []) if f.get("filename", "").endswith(".craft")]
        if not craft_files:
            return

        corp = find_user_corp(gid, builder_id)
        if not corp or not corp.get("channel_id"):
            log.info("Corp delivery skipped for %s: builder %d has no corp channel", contract_id, builder_id)
            return

        guild = _bot_instance.get_guild(gid)
        if guild is None:
            return
        channel = guild.get_channel(int(corp["channel_id"]))
        if channel is None:
            return

        cf = craft_files[0]
        try:
            data = await cdb.download_url(cf["url"])
        except Exception as exc:
            log.error("Corp delivery: could not download craft for %s: %s", contract_id, exc)
            return

        craft_name = (c.get("vessel_data") or {}).get("vessel_name") or cf["filename"][:-6]
        embed = discord.Embed(
            title="🏢 New craft delivered to the corporation",
            description=f"**{c.get('contractor_name', 'A member')}** completed a contract and added "
                        f"**{craft_name}** to {corp.get('name', 'the corp')}.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Mission", value=(c.get("mission") or "—")[:200], inline=False)
        embed.set_footer(text="Download the .craft, or hit Load to KSP to auto-install it.")
        from cogs.contractcraft import CorpCraftView
        craft_file = discord.File(io.BytesIO(data), filename=cf["filename"])
        await channel.send(embed=embed, file=craft_file, view=CorpCraftView(contract_id, gid))
        log.info("Corp delivery: posted craft %s to channel %s", contract_id, corp["channel_id"])
    except Exception as exc:
        log.error("Corp delivery failed for %s: %s", contract_id, exc)


async def _discord_notify_issuer(
    gid: int, issuer_id: int, contract_id: str,
    contract: dict, submitter_name: str, stored_files: list[dict],
    vessel_data: dict | None = None,
):
    """Post a submission notification to the issuer's Discord corp channel."""
    if _bot_instance is None:
        log.warning("Bot instance not set — cannot send Discord notification")
        return

    try:
        import discord
        from cogs.corps import _get_corp
        from cogs.contract_views import ContractReviewView

        # Find issuer's corp channel
        corp = _get_corp(gid, issuer_id)
        if not corp or not corp.get("channel_id"):
            log.warning("No corp channel for issuer %d — cannot notify", issuer_id)
            return

        guild = _bot_instance.get_guild(gid)
        if guild is None:
            return

        channel = guild.get_channel(int(corp["channel_id"]))
        if channel is None:
            return

        # Build embed
        mission = contract.get("mission", "Unknown mission")
        embed = discord.Embed(
            title="📤 Contract Submission Received",
            description=f"**{submitter_name}** submitted work for your contract.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Mission", value=mission[:200], inline=False)
        embed.add_field(name="Contract ID", value=contract_id, inline=True)
        embed.add_field(name="Payment", value=f"{contract.get('payment', 0)} KCoins", inline=True)

        # Add screenshot URL if available
        screenshots = [f for f in stored_files if f.get("content_type", "").startswith("image/")]
        if screenshots:
            embed.set_image(url=screenshots[0]["url"])

        # Add craft file info
        craft_files = [f for f in stored_files if f.get("filename", "").endswith(".craft")]
        if craft_files:
            embed.add_field(name="📎 Craft File", value=craft_files[0]["filename"], inline=True)

        embed.set_footer(text="Use the buttons below to accept or refuse this submission.")

        # A multi-craft submission ships several renders and one orbit diagram per
        # craft. Discord shows at most one image per embed, so add an embed for each
        # extra render (the first is already on the main embed) and one per orbit
        # diagram. Total embeds are capped at Discord's limit of 10.
        embeds = [embed]
        orbit_files: list = []

        for idx, rf in enumerate(screenshots):
            if idx == 0 or len(embeds) >= 10:
                continue   # first render is on the main embed
            re = discord.Embed(title=f"🚀 Craft Render {idx + 1}", color=discord.Color.blue())
            re.set_image(url=rf["url"])
            embeds.append(re)

        if vessel_data:
            try:
                from orbit_render import render_orbit

                # Active (contract) craft first, then any extras sent with it.
                snaps = []
                active_snap = vessel_data.get("active_vessel") or vessel_data
                if isinstance(active_snap, dict):
                    snaps.append(active_snap)
                for sv in (vessel_data.get("sent_vessels") or []):
                    if isinstance(sv, dict):
                        snaps.append(sv)

                for idx, snap in enumerate(snaps):
                    if len(embeds) >= 10:
                        break
                    try:
                        orbit_png = render_orbit(snap)
                    except Exception as exc:
                        log.warning("orbit render failed for vessel %d on %s: %s", idx, contract_id, exc)
                        continue
                    if not orbit_png:
                        continue
                    fname = f"orbit_{idx}.png"
                    orbit_files.append(discord.File(io.BytesIO(orbit_png), filename=fname))
                    vname = snap.get("vessel_name") or snap.get("vesselName") or "Vessel"
                    body = snap.get("body") or "—"
                    orbit_embed = discord.Embed(
                        title=f"🛰️ {vname} — Orbital Telemetry",
                        description=f"State around **{body}**.",
                        color=discord.Color.teal(),
                    )
                    orbit_embed.set_image(url=f"attachment://{fname}")
                    embeds.append(orbit_embed)
            except Exception as exc:
                log.warning("Failed to render orbit diagrams for %s: %s", contract_id, exc)

        # Attach review buttons (✅ Accept / ❌ Refuse) — uses the same
        # persistent view that the Discord-native contract flow uses
        view = ContractReviewView(contract_id, gid)

        # Mention the issuer
        issuer_mention = f"<@{issuer_id}>"
        send_kwargs: dict = {"content": issuer_mention, "embeds": embeds, "view": view}
        if orbit_files:
            send_kwargs["files"] = orbit_files
        await channel.send(**send_kwargs)
        log.info("Discord: Notified issuer %d in channel %s about submission", issuer_id, corp["channel_id"])

    except Exception as exc:
        log.error("Failed to send Discord notification to issuer: %s", exc)
