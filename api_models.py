"""
api_models.py – Pydantic models for KSP API request/response validation.
"""

from pydantic import BaseModel, Field
from typing import Optional


# ── Auth ─────────────────────────────────────────────────────────────────────

class LinkRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, description="6-digit link code from Discord")

class LinkResponse(BaseModel):
    token: str
    username: str
    guild_id: str
    user_id: str

class AuthError(BaseModel):
    detail: str


# ── User Profile ─────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    user_id: str
    username: str
    guild_id: str
    xp: int = 0
    level: int = 0
    balance: int = 0
    messages: int = 0
    unlocked_levels: list[int] = []
    currency_name: str = "KCoins"


# ── Missions ─────────────────────────────────────────────────────────────────

class Mission(BaseModel):
    id: int
    desc_en: str
    desc_tr: str
    difficulty: int
    category: str
    xp: int
    coins: int
    fine: int
    # AI-classified submission requirements (cached server-side)
    mission_type: str = "active_vessel"  # "craft_build" or "active_vessel"
    required_situation: Optional[str] = None  # KSP situation: ORBITING, LANDED, FLYING, etc.
    required_body: Optional[str] = None  # Celestial body: Kerbin, Mun, Duna, etc.

class WeeklyMissionsResponse(BaseModel):
    week_key: str
    missions: list[Mission]
    is_locked: bool
    closes_at: str  # ISO timestamp

class MissionSelectRequest(BaseModel):
    mission_id: int

class MissionSelectResponse(BaseModel):
    success: bool
    contract_id: Optional[str] = None
    message: str


# ── Contracts ────────────────────────────────────────────────────────────────

class RescueTarget(BaseModel):
    """Where stranded kerbals must be recovered from / delivered to.

    mode == "orbit"   → ap/pe define the target orbit (metres above the body
                        surface); margin_alt is the allowed +/- on each.
    mode == "surface" → lat/lon define the landing spot (degrees); margin_pos is
                        the allowed great-circle tolerance (degrees).
    is_modded is flagged by the issuer's client (it scans the real body list).
    """
    body: str
    mode: str = "orbit"  # "orbit" | "surface"
    ap: Optional[float] = None
    pe: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    margin_alt: float = 0.0
    margin_pos: float = 0.0
    is_modded: bool = False


class ContractSummary(BaseModel):
    contract_id: str
    mission: str
    issuer_name: str
    contractor_name: str
    payment: int
    fine: int
    due_date: str
    status: str
    created_at: Optional[str] = None
    is_bot_issued: bool = False
    is_outgoing: bool = False  # True when the current user is the issuer (sent, not received)
    modlist: Optional[str] = None  # Comma-separated mod folder names from issuer's KSP client
    # Classification (from mission)
    mission_type: str = "active_vessel"
    required_situation: Optional[str] = None
    required_body: Optional[str] = None
    # Flag-design contracts: watermarked preview shown before acceptance.
    flag_preview_url: Optional[str] = None
    # Rescue-mission fields (only set when mission_type == "rescue")
    rescue_target: Optional[RescueTarget] = None
    rescue_kerbals: list[str] = []  # renamed names the rescuer must recover
    is_modded_target: bool = False

class ContractListResponse(BaseModel):
    contracts: list[ContractSummary]

class ContractAcceptResponse(BaseModel):
    success: bool
    message: str
    # Set on rescue accept so the rescuer's client can spawn the wreck.
    rescue_vessel_node_url: Optional[str] = None
    rescue_target: Optional[RescueTarget] = None
    rescue_kerbals: list[str] = []


# ── Corporations ─────────────────────────────────────────────────────────────

class CorpInfo(BaseModel):
    owner_id: str
    owner_name: str
    corp_name: str

class CorpListResponse(BaseModel):
    corps: list[CorpInfo]

class ContractCreateRequest(BaseModel):
    contractor_id: str  # Corp owner's user ID
    mission: str = Field(..., min_length=3, max_length=500)
    payment: int = Field(..., gt=0)
    fine: int = Field(default=0, ge=0)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    modlist: Optional[str] = None  # Comma-separated list of loaded assembly names
    # "auto" keeps the existing AI classification; "craft_build" / "active_vessel"
    # force the type and skip AI. (Rescue contracts use the separate multipart
    # /contracts/create_rescue endpoint.)
    contract_type: str = "auto"


class ContractReviewRequest(BaseModel):
    approve: bool  # True = accept the submission, False = refuse (→ dispute)


class ContractDisputeRequest(BaseModel):
    # Contractor's response to a refused submission, mirroring the Discord
    # DisputeView buttons.
    action: str  # "settle" | "more_time" | "pay_fine" | "sue"
    # Required for "more_time" on human-issued contracts (YYYY-MM-DD).
    new_date: Optional[str] = None


# ── Submissions ──────────────────────────────────────────────────────────────

class SubmissionResult(BaseModel):
    success: bool
    message: str
    review_status: Optional[str] = None  # "approved", "refused", "pending"
    reason: Optional[str] = None
    xp_awarded: int = 0
    coins_awarded: int = 0

class VesselSnapshot(BaseModel):
    """Vessel data collected from KSP flight scene."""
    vessel_name: str
    vessel_type: str  # "Ship", "Station", "Probe", etc.
    situation: str  # "ORBITING", "LANDED", "FLYING", etc.
    body: str  # "Kerbin", "Mun", etc.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    # Orbital elements (if orbiting)
    sma: Optional[float] = None
    eccentricity: Optional[float] = None
    inclination: Optional[float] = None
    # Craft metadata
    part_count: int = 0
    total_mass: float = 0.0
    total_cost: float = 0.0
    crew_count: int = 0

class FlightSubmission(BaseModel):
    """Submitted alongside craft/screenshot files for flight missions."""
    contract_id: str
    active_vessel: VesselSnapshot
    nearby_vessels: list[VesselSnapshot] = []
    modlist: Optional[str] = None  # Comma-separated list of loaded assembly names


# ── Marketplace ──────────────────────────────────────────────────────────────

class MarketplaceListResult(BaseModel):
    success: bool
    message: str
    listing_id: Optional[str] = None

class MarketplaceListing(BaseModel):
    listing_id: str
    seller_id: str
    seller_name: str
    craft_name: str
    craft_type: str
    part_count: int
    mass: float
    cost: float
    price: int
    sales_count: int = 0
    created_at: Optional[str] = None

class MarketplaceListingsResponse(BaseModel):
    listings: list[MarketplaceListing]


# ── Notifications ────────────────────────────────────────────────────────────

class Notification(BaseModel):
    id: str
    type: str  # "contract_incoming", "review_result", "reward", "mission_update"
    title: str
    message: str
    timestamp: str
    read: bool = False
    data: Optional[dict] = None  # Extra context (contract_id, reward amounts, etc.)

class NotificationsResponse(BaseModel):
    notifications: list[Notification]
    unread_count: int
