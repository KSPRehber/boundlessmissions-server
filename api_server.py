"""
api_server.py – FastAPI REST API for KSP mod ↔ Discord bot bridge.

Runs inside the bot process via uvicorn. All endpoints require a valid
session token (Authorization: Bearer <token>) except /auth/link.

No API keys, Firebase creds, or secrets are exposed to clients.
"""

import asyncio
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    FastAPI, Depends, HTTPException, Header, UploadFile, File, Form,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import firestore

import settings
from config import cfg
from api_auth import (
    validate_link_code, create_session_token, verify_session_token,
)
from api_models import (
    LinkRequest, LinkResponse,
    UserProfile,
    WeeklyMissionsResponse, Mission, MissionSelectRequest, MissionSelectResponse,
    ContractSummary, ContractListResponse, ContractAcceptResponse,
    CorpInfo, CorpListResponse, ContractCreateRequest, ContractReviewRequest,
    ContractDisputeRequest,
    SubmissionResult, FlightSubmission, VesselSnapshot,
    Notification, NotificationsResponse,
)
from data.store import store, _db, _storage_bucket
from data import contracts as cdb

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
    return getattr(cfg, "API_SECRET_KEY", "gk-default-secret-change-me")


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

@app.post("/api/v1/auth/link", response_model=LinkResponse)
async def auth_link(req: LinkRequest):
    """Exchange a 6-digit link code for a session token."""
    result = validate_link_code(req.code)
    if result is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link code")

    token = create_session_token(
        result["guild_id"], result["user_id"], result["username"],
        _get_api_secret(),
    )

    return LinkResponse(
        token=token,
        username=result["username"],
        guild_id=result["guild_id"],
        user_id=result["user_id"],
    )


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
            "3. required_body: celestial body name or null\n\n"
            "Rules:\n"
            "- Missions about designing, building, constructing vessels = 'craft_build'\n"
            "- Turkish 'tasarım/tasarla' = design = 'craft_build'\n"
            "- Turkish 'inşa/yap/kur' = build = 'craft_build'\n"
            "- Turkish 'uçak' = airplane, 'roket' = rocket (design context = craft_build)\n"
            "- Missions about orbiting, landing, reaching a place = 'active_vessel'\n\n"
            "Return ONLY valid JSON:\n"
            '{"mission_type": "...", "required_situation": "...", "required_body": "..."}'
        )

        try:
            response = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=256),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            result = json.loads(raw.strip())
            log.info("AI classified contract %s: %s", contract_id, result.get("mission_type"))
        except Exception as exc:
            log.error("AI single-contract classification failed: %s", exc)
            result = _classify_text_heuristic(mission_text)
    else:
        result = _classify_text_heuristic(mission_text)

    # Cache result back to the contract document
    try:
        cdb.update_contract(gid, contract_id,
            mission_type=result.get("mission_type", "active_vessel"),
            required_situation=result.get("required_situation"),
            required_body=result.get("required_body"),
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
    # Store classification on the contract so KSP can enforce rules
    cdb.update_contract(gid, c["contract_id"],
        status=cdb.ACTIVE,
        mission_type=mission.get("mission_type", "active_vessel"),
        required_situation=mission.get("required_situation"),
        required_body=mission.get("required_body"),
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

            if not mission_type:
                cls = await _classify_single_contract(gid, c["contract_id"], c["mission"])
                mission_type = cls.get("mission_type", "active_vessel")
                req_sit = cls.get("required_situation")
                req_body = cls.get("required_body")

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

    return ContractAcceptResponse(success=True, message="Contract cancelled. Escrow refunded.")

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

    # AI-classify the contract
    cls = await _classify_single_contract(gid, c["contract_id"], req.mission)

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
    modlist: Optional[str] = Form(None),
    used_modlist: Optional[str] = Form(None),
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

    # Screenshots
    for ss in [screenshot1, screenshot2, screenshot3]:
        if ss:
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

    # Upload vessel node (full vessel state for transfer) to Storage
    if vessel_node:
        vn_data = await vessel_node.read()
        try:
            vn_url = await cdb.upload_to_storage(
                contract_id, "vessel_node.cfg", vn_data, "application/gzip"
            )
            update_fields["vessel_node_url"] = vn_url
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
    """Get craft file download URL from a completed contract."""
    gid = int(user["guild_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    # Only issuer can download craft from completed contracts
    if c.get("status") != cdb.COMPLETED:
        raise HTTPException(status_code=400, detail="Contract not completed yet")

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

        # Orbit telemetry diagram (rendered from the vessel data captured at
        # submission). Sent as a second embed so it sits below the screenshot.
        embeds = [embed]
        orbit_file = None
        if vessel_data:
            try:
                from orbit_render import render_orbit
                orbit_png = render_orbit(vessel_data)
                if orbit_png:
                    orbit_file = discord.File(io.BytesIO(orbit_png), filename="orbit.png")
                    body = vessel_data.get("body") or "—"
                    orbit_embed = discord.Embed(
                        title="🛰️ Orbital Telemetry",
                        description=f"Submitted vessel state around **{body}**.",
                        color=discord.Color.teal(),
                    )
                    orbit_embed.set_image(url="attachment://orbit.png")
                    embeds.append(orbit_embed)
            except Exception as exc:
                log.warning("Failed to render orbit diagram for %s: %s", contract_id, exc)

        # Attach review buttons (✅ Accept / ❌ Refuse) — uses the same
        # persistent view that the Discord-native contract flow uses
        view = ContractReviewView(contract_id, gid)

        # Mention the issuer
        issuer_mention = f"<@{issuer_id}>"
        send_kwargs: dict = {"content": issuer_mention, "embeds": embeds, "view": view}
        if orbit_file is not None:
            send_kwargs["file"] = orbit_file
        await channel.send(**send_kwargs)
        log.info("Discord: Notified issuer %d in channel %s about submission", issuer_id, corp["channel_id"])

    except Exception as exc:
        log.error("Failed to send Discord notification to issuer: %s", exc)
