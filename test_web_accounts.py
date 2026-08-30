"""Offline exercise of the website account layer: sign-in, onboarding, panel links.

Runs the real FastAPI app through TestClient with Firebase's token verifier and the
accounts store replaced. What it is checking is the set of refusals — an unverified
email, an unreadable account, someone else's pending approval — because those are
the paths where getting it wrong hands out an account that isn't the caller's.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

import api_server
import api_auth
from data import accounts as acc

# ── stub the accounts store (a dict, not Firestore) ──────────────────────────

ACCOUNTS: dict[str, dict] = {}
RESOLVE_FAILS = False


def _account_for_firebase(uid):
    if RESOLVE_FAILS:
        return None
    for aid, a in ACCOUNTS.items():
        if a.get("firebase_uid") == str(uid):
            return aid
    return acc.firebase_account_id(uid)


def _ensure_firebase_account(uid, *, email="", display_name=""):
    aid = _account_for_firebase(uid)
    if aid is None:
        return None
    if aid not in ACCOUNTS:
        ACCOUNTS[aid] = {"account_id": aid, "firebase_uid": str(uid), "email": email,
                         "username": "", "display_name": display_name, "avatar_url": ""}
    return ACCOUNTS[aid]


CLAIMED: dict[str, str] = {}


def _claim_username(aid, name):
    problem = acc.validate_username(name)
    if problem:
        return False, problem
    key = acc.normalize_username(name)
    a = ACCOUNTS.get(str(aid))
    if a is None:
        return False, "Couldn't find your account."
    if a.get("username"):
        if acc.normalize_username(a["username"]) == key:
            return True, a["username"]
        return False, "Your username is already set and can't be changed."
    if key in CLAIMED:
        return False, "That username is taken."
    CLAIMED[key] = str(aid)
    a["username"] = str(name).strip()
    return True, a["username"]


def _set_display_name(aid, name):
    raw = str(name or "").strip()
    if not raw:
        return False, "Display names can't be empty."
    if len(raw) > acc.DISPLAY_NAME_MAX:
        return False, "too long"
    ACCOUNTS[str(aid)]["display_name"] = raw
    return True, raw


api_server.accounts.get_account = lambda aid: ACCOUNTS.get(str(aid))
api_server.accounts.account_for_firebase = _account_for_firebase
api_server.accounts.ensure_firebase_account = _ensure_firebase_account
api_server.accounts.claim_username = _claim_username
api_server.accounts.set_display_name = _set_display_name
api_server.accounts.ensure_discord_account = lambda *a, **k: None
api_server.sign_stored = lambda v, ttl=0: v

# ── keep the corp writes off the real database ──────────────────────────────
#
# Claiming a username and setting a display name now create and sync a corp
# record, and `cogs.corps` writes through `data.store`'s REAL Firestore handle —
# stubbing `accounts.*` above does not cover it. The endpoints import these at
# call time, so patching the module attribute here is enough.
#
# This is what leaked "Jebediah Space Agency" into the live project: a test
# fixture that showed up in the in-game player picker.
import cogs.corps as _corps
_corps.ensure_corp_record_for_account = lambda *a, **k: False
_corps.sync_web_corp_profile = lambda *a, **k: False

# ── stub Firebase token verification ─────────────────────────────────────────

TOKENS: dict[str, dict] = {}


class _FakeAuth:
    @staticmethod
    def verify_id_token(token, check_revoked=False):
        if token not in TOKENS:
            raise ValueError("bad token")
        return TOKENS[token]


import firebase_admin
firebase_admin.auth = _FakeAuth()

# ── stub the link-code / approval collections ────────────────────────────────

CODES: dict[str, dict] = {}
CHALLENGES: dict[str, dict] = {}


def _gen_code(account_id, guild_id, display_name):
    code = "654321"
    expires = time.time() + api_auth.PANEL_LINK_CODE_LIFETIME
    CODES[code] = {"guild_id": str(guild_id), "user_id": str(account_id),
                   "username": display_name, "source": api_auth.SOURCE_PANEL,
                   "expires_at": expires}
    return code, expires


def _pending(account_id):
    now = time.time()
    for cid, d in CHALLENGES.items():
        if str(d.get("user_id")) != str(account_id):
            continue
        if d.get("source") != api_auth.SOURCE_PANEL:
            continue
        if d.get("status") != "pending" or now > d.get("expires_at", 0):
            continue
        out = dict(d)
        out["challenge_id"] = cid
        return out
    return None


def _resolve(cid, acting, approve):
    d = CHALLENGES.get(cid)
    if not d or str(d.get("user_id")) != str(acting) or d.get("status") != "pending":
        return False
    d["status"] = "approved" if approve else "denied"
    return True


api_server.generate_account_link_code = _gen_code
api_server.pending_panel_approval = _pending
api_server.resolve_approval = _resolve

# ── auth: mint real tokens against the real signer ───────────────────────────

SECRET = "x" * 48
api_server._get_api_secret = lambda: SECRET
api_server.verify_session_token = lambda tok, sec: api_auth.verify_session_token(tok, SECRET)
api_server.enforce_not_suspended = lambda *a, **k: None


def token_for(account_id):
    return api_auth.create_session_token("0", str(account_id), "T", SECRET, aud=api_auth.AUD_WEB)


api_auth._get_token_version = lambda uid: 0

# ── keep the token minter off the real database ─────────────────────────────
#
# `create_session_token` WRITES `ksp_sessions/{uid}` through api_auth's own
# Firestore handle. Stubbing `api_server.verify_session_token` does not touch that
# — so before this, every `auth(...)` call in this file quietly created a document
# in the live project. Tests must not write to production, so the collection is
# replaced with a dict here.
_SESSIONS: dict[str, dict] = {}


class _SessDoc:
    def __init__(self, key): self._k = key

    def get(self):
        payload = _SESSIONS.get(self._k)
        return type("_S", (), {"exists": payload is not None,
                               "to_dict": staticmethod(lambda: dict(payload or {}))})()

    def set(self, payload, merge=False):
        if merge:
            _SESSIONS.setdefault(self._k, {}).update(payload)
        else:
            _SESSIONS[self._k] = dict(payload)

    def update(self, payload):
        _SESSIONS.setdefault(self._k, {}).update(payload)


api_auth._sessions_col = lambda: type("_C", (), {
    "document": staticmethod(lambda k: _SessDoc(str(k))),
    "stream": staticmethod(lambda: iter(())),
})()


client = TestClient(api_server.app, raise_server_exceptions=False)

# ── assertions ───────────────────────────────────────────────────────────────
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def auth(account_id):
    return {"Authorization": f"Bearer {token_for(account_id)}"}


def reset():
    ACCOUNTS.clear(); CLAIMED.clear(); TOKENS.clear()
    CODES.clear(); CHALLENGES.clear()
    global RESOLVE_FAILS
    RESOLVE_FAILS = False
    api_server._rate_buckets.clear() if hasattr(api_server, "_rate_buckets") else None


def main():
    global RESOLVE_FAILS
    UID = "FirebaseUid000000000000001"
    AID = acc.firebase_account_id(UID)

    print("\nsign-in")
    reset()
    TOKENS["good"] = {"uid": UID, "email": "jeb@example.com", "email_verified": True,
                      "name": "Jeb", "firebase": {"sign_in_provider": "google.com"}}
    r = client.post("/api/v1/web/auth/signin", json={"id_token": "good"})
    check("a verified Google sign-in succeeds", r.status_code == 200, r.text)
    body = r.json()
    check("mints a session token", bool(body.get("token")))
    check("reports the account id", body.get("account_id") == AID, body)
    check("and sends the user to onboarding", body.get("needs_onboarding") is True)
    check("the token carries the ACCOUNT id, which is what every endpoint keys on",
          api_auth.verify_session_token(body["token"], SECRET)["user_id"] == AID)

    r = client.post("/api/v1/web/auth/signin", json={"id_token": "nope"})
    check("an unverifiable token is refused", r.status_code == 401, r.status_code)

    print("\nsign-in: unverified email is refused")
    reset()
    TOKENS["unver"] = {"uid": UID, "email": "jeb@example.com", "email_verified": False,
                       "firebase": {"sign_in_provider": "password"}}
    r = client.post("/api/v1/web/auth/signin", json={"id_token": "unver"})
    check("registering with an address you don't own gets you nowhere",
          r.status_code == 403, r.status_code)
    check("and no account was created", ACCOUNTS == {}, ACCOUNTS)

    print("\nsign-in: an unreadable account is refused, never guessed")
    reset()
    TOKENS["good"] = {"uid": UID, "email": "j@e.com", "email_verified": True,
                      "firebase": {"sign_in_provider": "google.com"}}
    RESOLVE_FAILS = True
    r = client.post("/api/v1/web/auth/signin", json={"id_token": "good"})
    check("a failed resolution is 503, not a fresh empty wallet",
          r.status_code == 503, r.status_code)
    RESOLVE_FAILS = False

    print("\naccount profile")
    reset()
    TOKENS["good"] = {"uid": UID, "email": "j@e.com", "email_verified": True,
                      "name": "Jeb", "firebase": {"sign_in_provider": "google.com"}}
    client.post("/api/v1/web/auth/signin", json={"id_token": "good"})
    r = client.get("/api/v1/web/account", headers=auth(AID))
    check("reads back", r.status_code == 200, r.text)
    check("needs onboarding until a username is claimed",
          r.json()["needs_onboarding"] is True)
    check("a web-only account has no Discord", r.json()["has_discord"] is False)

    r = client.get("/api/v1/web/account")
    check("no token is refused", r.status_code == 401, r.status_code)
    r = client.get("/api/v1/web/account", headers=auth("a_ghost"))
    check("a session outliving its account is 401, not 404 — the page must treat "
          "it as signed out and not as a stripped-down account",
          r.status_code == 401, r.status_code)

    print("\nonboarding: the permanent username")
    r = client.post("/api/v1/web/account/username", json={"username": "Jebediah"},
                    headers=auth(AID))
    check("claims it", r.status_code == 200, r.text)
    r = client.get("/api/v1/web/account", headers=auth(AID))
    check("onboarding is done", r.json()["needs_onboarding"] is False)
    check("and the username is shown", r.json()["username"] == "Jebediah")

    r = client.post("/api/v1/web/account/username", json={"username": "Jebediah"},
                    headers=auth(AID))
    check("re-sending the same name is idempotent, not an error",
          r.status_code == 200, r.text)
    r = client.post("/api/v1/web/account/username", json={"username": "Bill"},
                    headers=auth(AID))
    check("but it cannot be changed", r.status_code == 409, r.status_code)
    r = client.post("/api/v1/web/account/username", json={"username": "admin"},
                    headers=auth(AID))
    check("reserved names are refused", r.status_code == 409, r.status_code)

    print("\ndisplay name")
    r = client.post("/api/v1/web/account/display_name",
                    json={"display_name": "Commander Jeb"}, headers=auth(AID))
    check("changes freely", r.status_code == 200, r.text)
    r = client.get("/api/v1/web/account", headers=auth(AID))
    check("and shows at once, not when the 30-day token turns over",
          r.json()["display_name"] == "Commander Jeb")
    r = client.post("/api/v1/web/account/display_name",
                    json={"display_name": "   "}, headers=auth(AID))
    # Whitespace clears pydantic's min_length and is caught after trimming, so this
    # is the handler's own 400 with a sentence — better than a generic 422 body.
    check("whitespace-only is refused, with a reason",
          r.status_code == 400 and "empty" in r.json()["detail"].lower(), r.text)

    print("\nan authenticator gates LINKING, not just sign-in")
    # The point of the Discord DM approval was always that a link code alone is not
    # enough. An account with TOTP now gets that guarantee without needing a DM at
    # all — which is what makes it work for a player with DMs closed, and for one
    # with no Discord whatsoever.
    from data import twofa as _twofa
    api_server.twofa.is_enabled = lambda aid: str(aid) == AID
    made = {}
    api_server.twofa.create_login_challenge = (
        lambda aid, payload=None: made.setdefault("cid", "chal-totp") and None
        or made.update({"aid": aid, "payload": payload}) or "chal-totp")

    CODES["111111"] = {"guild_id": "0", "user_id": AID, "username": "Jeb",
                       "source": api_auth.SOURCE_PANEL,
                       "expires_at": time.time() + 600}
    api_server.validate_link_code = lambda code: (
        {**CODES[code], "source": CODES[code]["source"]} if code in CODES else None)

    r = client.post("/api/v1/auth/link", json={"code": "111111"})
    check("the link stops for a code instead of issuing a token",
          r.status_code == 200 and r.json().get("status") == "totp_required", r.text)
    check("no token was handed out", not r.json().get("token"))
    check("and the spent link result rides on the challenge",
          made.get("payload", {}).get("user_id") == AID, made)

    api_server.twofa.is_enabled = lambda aid: False
    r = client.post("/api/v1/auth/link", json={"code": "111111"})
    check("an account without one is unaffected",
          r.json().get("status") != "totp_required", r.text)

    print("\nlinking a KSP install from the panel")
    r = client.post("/api/v1/web/account/ksp/code", headers=auth(AID))
    check("mints a code", r.status_code == 200 and len(r.json()["code"]) == 6, r.text)
    check("with a countdown longer than the Discord one's 3 minutes",
          r.json()["expires_in"] > api_auth.LINK_CODE_LIFETIME, r.json())

    r = client.get("/api/v1/web/account/ksp/pending", headers=auth(AID))
    check("nothing to approve yet", r.json()["pending"] is False)

    CHALLENGES["chal1"] = {"user_id": AID, "source": api_auth.SOURCE_PANEL,
                           "status": "pending", "client_ip": "1.2.3.4",
                           "device_id": "abcdef0123456789",
                           "created_at": "now", "expires_at": time.time() + 120}
    r = client.get("/api/v1/web/account/ksp/pending", headers=auth(AID))
    check("a waiting KSP client shows up", r.json()["pending"] is True, r.json())
    check("with something to judge it by", r.json()["client_ip"] == "1.2.3.4")
    check("and the device id is truncated, not handed over whole",
          r.json()["device_id"] == "abcdef01")

    other = acc.firebase_account_id("SomebodyElse00000000000001")
    ACCOUNTS[other] = {"account_id": other, "username": "bill", "display_name": "Bill"}
    r = client.get("/api/v1/web/account/ksp/pending", headers=auth(other))
    check("another account sees nothing of it", r.json()["pending"] is False)
    r = client.post("/api/v1/web/account/ksp/approve",
                    json={"challenge_id": "chal1", "approve": True}, headers=auth(other))
    check("and cannot answer it even holding the challenge id",
          r.status_code == 409, r.status_code)
    check("it is still pending", CHALLENGES["chal1"]["status"] == "pending")

    r = client.post("/api/v1/web/account/ksp/approve",
                    json={"challenge_id": "chal1", "approve": True}, headers=auth(AID))
    check("the owner can approve it", r.status_code == 200, r.text)
    check("which is what releases the token to KSP",
          CHALLENGES["chal1"]["status"] == "approved")
    r = client.post("/api/v1/web/account/ksp/approve",
                    json={"challenge_id": "chal1", "approve": True}, headers=auth(AID))
    check("answering twice is refused", r.status_code == 409, r.status_code)

    print("\nlink codes carry where they came from")
    reset()
    code, _ = _gen_code("a_someone", "0", "Someone")
    check("a panel code is marked panel",
          CODES[code]["source"] == api_auth.SOURCE_PANEL)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())
