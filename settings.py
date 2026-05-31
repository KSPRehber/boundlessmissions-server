# ─────────────────────────────────────────────────────────────────────────────
#  settings.py – PUBLIC tunable settings (safe to commit, no secrets here)
#
#  Unlike .env, these are gameplay / balance values anyone can see.
#  Adjust these to tune the XP economy for your server.
# ─────────────────────────────────────────────────────────────────────────────

# ── Moderation & Roles ───────────────────────────────────────────────────────

# Role ID that grants access to moderation commands (/kick, /ban, /gk setchannel, etc.)
# If set to None, users must have Discord's built-in Kick Members or Admin permissions.
MOD_ROLE_ID: int | None = 1492234876273823916

# ── XP System ────────────────────────────────────────────────────────────────

# XP awarded per qualifying message
XP_PER_MESSAGE = 15

# Random bonus range added on top (0 = no randomness)
XP_BONUS_MIN = 0
XP_BONUS_MAX = 10

# Cooldown in seconds between XP-eligible messages (prevents spam farming)
XP_COOLDOWN_SECONDS = 60

# XP multiplier for server boosters (2.0 = double XP)
BOOSTER_XP_MULTIPLIER = 2.0

# Channels where XP is NOT awarded (by channel ID)
# Example: XP_BLACKLISTED_CHANNELS = [123456789, 987654321]
XP_BLACKLISTED_CHANNELS: list[int] = []

# ── Leveling ─────────────────────────────────────────────────────────────────

# Formula: XP needed for level N = BASE * (N ^ EXPONENT)
LEVEL_XP_BASE = 100
LEVEL_XP_EXPONENT = 1.5

# Whether to announce level-ups in the channel where it happened
ANNOUNCE_LEVEL_UP = True

# Optional: dedicated channel ID for level-up announcements (None = same channel)
LEVEL_UP_CHANNEL_ID: int | None = None

# ── Economy ──────────────────────────────────────────────────────────────────

# Starting balance for new users
STARTING_BALANCE = 0
CURRENCY_NAME = "KCoins"
CURRENCY_SYMBOL = "<:KCoin:1510200111253291258>"

# KCoins awarded per level-up
LEVEL_UP_REWARD = 200

# Minimum transfer amount for /pay
MIN_TRANSFER = 1

# ── Leaderboard ──────────────────────────────────────────────────────────────

# How many users to show on the leaderboard
LEADERBOARD_PAGE_SIZE = 10

# ── Screenshot Rewards ───────────────────────────────────────────────────────

# XP awarded per difficulty point (e.g. difficulty 7 × 50 = 350 XP)
SCREENSHOT_XP_PER_DIFFICULTY = 50

# KCoins awarded per difficulty point (e.g. difficulty 7 × 30 = 210 KCoins)
SCREENSHOT_COINS_PER_DIFFICULTY = 30

# ── Corporations ─────────────────────────────────────────────────────────────

# Discord category ID where corp channels are created
CORP_CATEGORY_ID = 1492379906925924352

# ── Contracts ────────────────────────────────────────────────────────────────

# Max active contracts a user can have at once (as either issuer or contractor)
MAX_ACTIVE_CONTRACTS_PER_USER = 5

# Channel ID where mod escalations ("sue" button) are posted.
# Set to None to disable suing. Must be set for the sue flow to work.
CONTRACT_MOD_CHANNEL_ID: int | None = None

# Allow users to send contracts to themselves (for testing only!)
CONTRACT_ALLOW_SELF = False

# ── Weekly Missions ──────────────────────────────────────────────────────────

# Channel where the weekly missions embed is posted
WEEKLY_MISSIONS_CHANNEL_ID = 1510353237922938949

# Number of missions generated per week
WEEKLY_MISSIONS_COUNT = 20

# Rewards per difficulty point
WEEKLY_XP_PER_DIFFICULTY = 50
WEEKLY_COINS_PER_DIFFICULTY = 30

# Fine = 50% of money reward
WEEKLY_FINE_PERCENT = 50

# Allow mods to select missions even when the week is locked (e.g., Sundays)
WEEKLY_MISSIONS_MODS_IGNORE_LOCK = False

# ── Data Persistence ─────────────────────────────────────────────────────────

# Path to the JSON data file (relative to project root)
DATA_FILE = "data/users.json"

# How often to auto-save in-memory data to disk (seconds)
AUTO_SAVE_INTERVAL = 300  # 5 minutes

# ── KSP Achievement Levels ───────────────────────────────────────────────────

# Mapping of level integers (1-15) to a tuple of (Role ID, Title Name, Description)
LEVEL_ROLES = {
    1:  (1492381704948551740, "Level-1", "Kerbin Orbit"),
    2:  (1492382379329851422, "Level-2", "Mun Landing"),
    3:  (1492382794498703551, "Level-3", "Docking (Space Stations are also considered to be on this level)"),
    4:  (1492382733769506876, "Level-4", "Duna Landing"),
    5:  (1492383069141864488, "Level-5", "RSS Earth Orbit"),
    6:  (1492384757139378197, "Level-6", "Eve Landing"),
    7:  (1492957576621719693, "Level-7", "Asteroid Redirect"),
    8:  (1492383446566310081, "Level-8", "RSS Moon Landing"),
    9:  (1492383547519012934, "Level-9", "Jool 5"),
    10: (1492383718357340362, "Level-10", "Interstellar Mission"),
    11: (1492383914851827874, "Level-11", "RSS Mars"),
    12: (1498035194760790108, "Level-12", "RSS Venus Landing"),
    13: (1492384267798450277, "Level-13", "RSS Gas Giant"),
    14: (1498035361564065892, "Level-14", "Kerbol Grand Tour to all planets at once"),
    15: (1492384471775707146, "Level-15", "RSS Interstellar Mission"),
}
