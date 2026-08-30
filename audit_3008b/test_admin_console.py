"""The owner/admin console (/api/v1/web/admin/*): tier and guild scoping, the
404-before-validation contract, and what `admin_user_adjust` will accept.

Everything runs against the real FastAPI app with `get_web_user` overridden and a
fake Discord bot; Firestore-facing marketplace/version/policy calls are stubbed.
Nothing here writes to Firestore (`store` is used in memory only)."""
import types

from _h import check, section, finish, quiet

from fastapi.testclient import TestClient

import api_server
from api_server import app
from config import cfg
from data import guild_config
from data.store import store

quiet(api_server)

OWNER = "100000000000000001"
G1_ADMIN = "100000000000000002"      # holds the mapped admin role in guild 1 only
PLAIN = "100000000000000003"         # a valid session, no authority anywhere
VICTIM = "100000000000000009"
G1, G2 = 111, 222
ROLE1, ROLE2 = 5551, 5552

cfg.OWNER_ID = int(OWNER)


# ── fake Discord ─────────────────────────────────────────────────────────────
class Role:
    def __init__(self, rid, name="admin"):
        self.id, self.name, self.members, self.managed = rid, name, [], False
    def is_default(self): return False

class Member:
    def __init__(self, uid, roles=()):
        self.id, self._roles, self.bot = uid, set(roles), False
    def get_role(self, rid): return Role(rid) if rid in self._roles else None

class Guild:
    def __init__(self, gid, members, role_id):
        self.id, self.name, self.member_count = gid, f"guild{gid}", 10
        self._members = {m.id: m for m in members}
        self.role = Role(role_id)
        self.roles = [self.role]
        self.text_channels = []
        self.default_role = Role(0, "@everyone")
    def get_member(self, uid): return self._members.get(uid)
    def get_channel(self, cid): return None           # none of the fakes are TextChannels
    def get_role(self, rid): return self.role if rid == self.role.id else None

class Bot:
    def __init__(self, guilds): self.guilds = guilds
    def get_guild(self, gid): return next((g for g in self.guilds if g.id == gid), None)
    def get_user(self, uid): return None

bot = Bot([
    Guild(G1, [Member(int(G1_ADMIN), {ROLE1}), Member(int(PLAIN))], ROLE1),
    Guild(G2, [Member(int(G1_ADMIN)), Member(int(PLAIN))], ROLE2),
])
api_server._bot_instance = bot
guild_config.resolve_role = lambda g, key: g.role if key == "admin" else None

# ── stubs for everything that would reach Firestore ─────────────────────────
LISTINGS = {
    "L1": {"listing_id": "L1", "guild_id": str(G1), "craft_name": "one", "seller_id": VICTIM,
           "seller_name": "v", "price": 5, "status": "active", "created_at": "2026-01-01",
           "buyers": [], "mods": [], "parts": []},
    "L2": {"listing_id": "L2", "guild_id": str(G2), "craft_name": "two", "seller_id": VICTIM,
           "seller_name": "v", "price": 5, "status": "active", "created_at": "2026-01-02",
           "buyers": [], "mods": [], "parts": []},
}
UPDATES: list = []
api_server.mkt.get_listing = lambda gid, lid: dict(LISTINGS[lid]) if lid in LISTINGS else None
api_server.mkt.list_all = lambda: [dict(v) for v in LISTINGS.values()]
api_server.mkt.update_listing = lambda gid, lid, **f: UPDATES.append((lid, f))
api_server.mkt.delete_listing = lambda lid: UPDATES.append((lid, "DELETE"))
api_server.mkt.clear_auto_delisted = lambda lid: None
api_server.cbans.check_hashes = lambda h: None
api_server.mver.get_config = lambda: {"latest_version": "1.2.3", "latest_hash": "deadbeef" * 8,
                                      "has_dll": True, "updated_at": "x"}
api_server.policy.get_version = lambda: 3
api_server.suspensions.list_active = lambda: []
api_server.suspensions.get_active = lambda uid: None
api_server.accounts.discord_for_account = lambda aid: str(aid)
api_server._listing_to_model = lambda l, include_download=False: types.SimpleNamespace(
    model_dump=lambda: {"listing_id": l["listing_id"], "guild_id": l["guild_id"]})

CURRENT = {"uid": PLAIN}

async def _fake_web_user():
    return {"user_id": CURRENT["uid"], "username": "u" + CURRENT["uid"][-1],
            "guild_id": str(G1), "aud": api_server.AUD_WEB}

app.dependency_overrides[api_server.get_web_user] = _fake_web_user
client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer x"}

def as_user(uid):
    CURRENT["uid"] = uid


# ── tier gates ───────────────────────────────────────────────────────────────
section("tier gates: 404 to non-admins, guild-admin cannot reach owner routes")
as_user(PLAIN)
for path in ("/api/v1/web/admin/whoami", "/api/v1/web/admin/users", "/api/v1/web/admin/controls",
             "/api/v1/web/admin/listings", "/api/v1/web/admin/guilds", "/api/v1/web/admin/craftbans"):
    r = client.get(path, headers=H)
    check(f"plain user GET {path.rsplit('/',1)[1]} -> 404", r.status_code == 404, f"got {r.status_code}")

as_user(G1_ADMIN)
r = client.get("/api/v1/web/admin/whoami", headers=H)
check("guild admin whoami -> scoped to guild 1 only",
      r.status_code == 200 and r.json()["admin_guild_ids"] == [str(G1)] and not r.json()["is_owner"], r.text)
for path in ("/api/v1/web/admin/users", "/api/v1/web/admin/controls", "/api/v1/web/admin/modversion",
             "/api/v1/web/admin/costs", "/api/v1/web/admin/craftbans"):
    r = client.get(path, headers=H)
    check(f"guild admin GET {path.rsplit('/',1)[1]} -> 404", r.status_code == 404, f"got {r.status_code}")
for path, body in (("/api/v1/web/admin/users/%s/adjust" % VICTIM, {"balance_set": 1}),
                   ("/api/v1/web/admin/users/%s/suspend" % VICTIM, {"hours": 1, "reason": "x"}),
                   ("/api/v1/web/admin/message", {"user_id": VICTIM, "content": "hi"}),
                   ("/api/v1/web/admin/controls", {"device_binding_enabled": False}),
                   ("/api/v1/web/admin/policy/bump", {}),
                   ("/api/v1/web/admin/craftbans", {"hash": "ab"})):
    r = client.post(path, json=body, headers=H)
    check(f"guild admin POST {path.split('/admin/')[1]} -> 404", r.status_code == 404, f"got {r.status_code}")
r = client.delete(f"/api/v1/web/admin/users/{VICTIM}", headers=H)
check("guild admin DELETE user -> 404", r.status_code == 404, f"got {r.status_code}")


# ── guild scoping ────────────────────────────────────────────────────────────
section("guild scoping for a guild-1 admin")
as_user(G1_ADMIN)
r = client.get("/api/v1/web/admin/listings", headers=H)
ids = {l["listing_id"] for l in r.json()["listings"]}
check("listings: only guild-1 listings are returned", ids == {"L1"}, f"got {ids}")
r = client.patch("/api/v1/web/admin/listings/L2", json={"price": 1}, headers=H)
check("edit a guild-2 listing -> 404", r.status_code == 404 and not UPDATES, f"{r.status_code} {UPDATES}")
r = client.delete("/api/v1/web/admin/listings/L2", headers=H)
check("delete a guild-2 listing -> 404", r.status_code == 404 and not UPDATES, f"{r.status_code} {UPDATES}")
r = client.patch("/api/v1/web/admin/listings/L1", json={"price": 1}, headers=H)
check("edit a guild-1 listing -> allowed", r.status_code == 200 and UPDATES == [("L1", {"price": 1})], r.text)
UPDATES.clear()
r = client.get("/api/v1/web/admin/guilds", headers=H)
check("guild picker: guild 2 hidden", [g["id"] for g in r.json()["guilds"]] == [str(G1)], r.text)
r = client.post("/api/v1/web/admin/announce", headers=H,
                json={"guild_id": str(G2), "title": "t", "content": "c", "open_tickets": True})
check("announce into guild 2 (id in body) -> 404", r.status_code == 404, f"got {r.status_code}")
r = client.post("/api/v1/web/admin/announce", headers=H,
                json={"guild_id": str(G1), "title": "t", "content": "c", "open_tickets": True})
check("announce into guild 1 passes the scope gate (fails later on 'needs a role')",
      r.status_code == 422, f"got {r.status_code} {r.text}")
r = client.post("/api/v1/web/admin/channels/123/lock", headers=H,
                json={"guild_id": str(G2), "locked": True})
check("lock a channel with guild 2 in the body -> 404", r.status_code == 404, f"got {r.status_code}")
r = client.post("/api/v1/web/admin/channels/123/lock", headers=H,
                json={"guild_id": "not-a-number", "locked": True})
check("lock with a non-numeric guild id -> 404 (no 500)", r.status_code == 404, f"got {r.status_code}")


# ── what the guild tier is told ──────────────────────────────────────────────
section("overview: bot-wide facts served to the guild tier")
as_user(G1_ADMIN)
ov = client.get("/api/v1/web/admin/overview", headers=H).json()
leaked = [k for k in ("mod_version", "suspensions_active", "device_binding_enabled",
                      "version_check_enabled", "users", "policy_version") if k in ov]
check("guild admin overview carries no owner-only state (DLL hash, gate switches, "
      "suspension count, global user count)", not leaked,
      f"served to guild tier: {leaked}; latest_hash={ov.get('mod_version', {}).get('latest_hash', '')[:12]}…")


# ── 404 before validation ────────────────────────────────────────────────────
section("404-vs-422: validation must not run before the tier gate")
as_user(PLAIN)
r = client.post("/api/v1/web/admin/announce", json={"bogus": 1}, headers=H)
check("plain user, invalid announce body -> 404 (not 422)", r.status_code == 404, f"got {r.status_code}")
r = client.get("/api/v1/web/admin/users?limit=abc", headers=H)
check("plain user, invalid query -> 404 (not 422)", r.status_code == 404, f"got {r.status_code}")
r = client.post(f"/api/v1/web/admin/users/{VICTIM}/adjust", json={"balance_set": "abc"}, headers=H)
check("plain user, invalid adjust body -> 404 (not 422)", r.status_code == 404, f"got {r.status_code}")
r = client.post("/api/v1/web/admin/modversion/publish", json={"version": "1"}, headers=H)
check("plain user, JSON to the multipart publish route -> 404 (not 422)",
      r.status_code == 404, f"got {r.status_code}")
r = client.post("/api/v1/web/admin/announce", content=b"{not json",
                headers={**H, "Content-Type": "application/json"})
print(f"       (info) plain user, malformed JSON -> {r.status_code}; a bogus path -> "
      f"{client.post('/api/v1/web/admin/nope', content=b'{', headers=H).status_code}")


# ── admin_user_adjust: what the body will take ───────────────────────────────
section("admin_user_adjust: bounds")
as_user(OWNER)
store._users[VICTIM] = {"balance": 100, "xp": 0, "level": 0, "username": "victim"}
r = client.post(f"/api/v1/web/admin/users/{VICTIM}/adjust", json={"balance_set": -50}, headers=H)
check("balance_set below zero clamps to 0", r.status_code == 200 and store._users[VICTIM]["balance"] == 0, r.text)

r = client.post(f"/api/v1/web/admin/users/{VICTIM}/adjust", json={"balance_set": 2 ** 70}, headers=H)
huge_ok = r.status_code == 200 and store._users[VICTIM]["balance"] == 2 ** 70
from google.cloud.firestore_v1._helpers import encode_dict
try:
    encode_dict(store._users[VICTIM])
    encodable = True
except ValueError:
    encodable = False
check("a balance the store cannot flush is refused (int64 bound)", not (huge_ok and not encodable),
      "balance_set=2**70 accepted (200); firestore encode_dict raises ValueError, so the next "
      "store.save() batch fails and is retried forever, taking every other dirty user's write with it")
store._users[VICTIM]["balance"] = 0
store._dirty_users.discard(VICTIM)

r = client.post(f"/api/v1/web/admin/users/{VICTIM}/adjust", json={"balance_delta": 2 ** 70}, headers=H)
check("balance_delta is bounded", not (r.status_code == 200 and store._users[VICTIM]["balance"] == 2 ** 70),
      "balance_delta=2**70 accepted")
store._users[VICTIM]["balance"] = 0
store._dirty_users.discard(VICTIM)

# xp_set is not exercised through the endpoint: level_from_xp on a huge value
# hangs the event loop (see test_admin_slash.py); only the model bound is checked.
fields = api_server.AdminUserAdjust.model_fields
bounded = any(getattr(fields[k], "metadata", None) for k in ("balance_set", "balance_delta", "xp_set"))
check("AdminUserAdjust declares numeric bounds", bounded, "balance_set/balance_delta/xp_set are bare Optional[int]")

del store._users[VICTIM]
store._dirty_users.discard(VICTIM)
finish()
