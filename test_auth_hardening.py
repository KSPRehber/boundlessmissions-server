"""Offline automated checks for the auth-hardening work (2026-08-20).

No network: Firestore is replaced with in-memory fakes before any auth function
runs, so this exercises the REAL api_auth/api_server logic against the exact
failure modes the fixes target:

  [1] Signing-key rotation — tokens signed under the previous key still verify
      via the accept list; a key not on the list never does; `kid` names the key.
  [2] Device-cache fail-open — a Firestore outage must not let trust-on-first-use
      permanently adopt whatever device happened to be asking (the 2am bug), and
      must not cache the empty guess.
  [3] Device removal/listing — the approval prompt is reversible: ArrayRemove
      path, metadata cleanup, immediate cache effect.
  [4] Link-code brute force — wrong codes lock out the address that sent them (no global purge).
  [5] Source guards — missing-Authorization is our 401 (not 422), the WS
      takes tickets only (no ?token= fallback), device reports are ownership-checked, the
      ban listener exists and every verify call site uses the accept list.

Run:  ./.venv/bin/python test_auth_hardening.py
"""
import base64
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api_auth

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


# ── In-memory Firestore fakes ────────────────────────────────────────────────

def _sentinel_name(v):
    return type(v).__name__


class FakeSnap:
    def __init__(self, data):
        self._d = data

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class FakeDoc:
    def __init__(self, col, key):
        self.col, self.key = col, key

    def get(self):
        if self.col.fail:
            raise RuntimeError("simulated Firestore outage")
        return FakeSnap(self.col.docs.get(self.key))

    def _apply(self, target, data):
        for k, v in data.items():
            if _sentinel_name(v) == "ArrayUnion":
                cur = list(target.get(k, []) or [])
                for x in v.values:
                    if x not in cur:
                        cur.append(x)
                target[k] = cur
            elif _sentinel_name(v) == "ArrayRemove":
                target[k] = [x for x in (target.get(k, []) or []) if x not in v.values]
            elif isinstance(v, dict) and isinstance(target.get(k), dict):
                self._apply(target[k], v)  # merge semantics for nested maps
            else:
                target[k] = v

    def set(self, data, merge=False):
        if self.col.fail:
            raise RuntimeError("simulated Firestore outage")
        if merge and self.key in self.col.docs:
            self._apply(self.col.docs[self.key], data)
        else:
            target = {}
            self._apply(target, data)
            self.col.docs[self.key] = target

    def update(self, data):
        if self.col.fail:
            raise RuntimeError("simulated Firestore outage")
        target = self.col.docs.setdefault(self.key, {})
        for k, v in data.items():
            # Support the one field-path shape the code writes: device_meta.`id`
            # (always paired with DELETE_FIELD in remove_allowed_device).
            m = re.fullmatch(r"device_meta\.`(.+)`", k)
            if m:
                target.setdefault("device_meta", {}).pop(m.group(1), None)
                continue
            self._apply(target, {k: v})

    def delete(self):
        self.col.docs.pop(self.key, None)

    # Query-result compatibility
    @property
    def reference(self):
        return self

    @property
    def id(self):
        return self.key


class FakeCol:
    def __init__(self):
        self.docs = {}
        self.fail = False

    def document(self, key):
        return FakeDoc(self, key)

    def stream(self):
        if self.fail:
            raise RuntimeError("simulated Firestore outage")
        for k in list(self.docs.keys()):
            d = FakeDoc(self, k)
            yield d

    def where(self, *a, **kw):
        col = self

        class _Q:
            def stream(self_inner):
                return iter(())
        return _Q()


sessions = FakeCol()
link_codes = FakeCol()
api_auth._sessions_col = lambda: sessions
api_auth._link_codes_col = lambda: link_codes
api_auth._token_versions.clear()
api_auth._allowed_devices.clear()


# ── [1] Key rotation ─────────────────────────────────────────────────────────

print("\n[1] Signing-key rotation (accept-list verification)")
OLD, NEW = "old-secret-abcdef", "new-secret-uvwxyz"
tok = api_auth.create_session_token("g1", "42", "player", OLD)
check("token verifies under its own key", api_auth.verify_session_token(tok, OLD) is not None)
check("token verifies via accept list [NEW, OLD]",
      api_auth.verify_session_token(tok, [NEW, OLD]) is not None)
check("token rejected by [NEW] alone", api_auth.verify_session_token(tok, [NEW]) is None)
check("garbage rejected", api_auth.verify_session_token("x.y", [NEW, OLD]) is None)
check("empty accept list rejects", api_auth.verify_session_token(tok, []) is None)
payload = json.loads(base64.urlsafe_b64decode(tok.split(".")[0]))
check("payload carries kid of the signing key", payload.get("kid") == api_auth.key_id(OLD))
check("kid is non-reversing (8 hex chars)", re.fullmatch(r"[0-9a-f]{8}", payload["kid"]) is not None)
new_tok = api_auth.create_session_token("g1", "42", "player", NEW)
check("post-rotation tokens carry the new kid",
      json.loads(base64.urlsafe_b64decode(new_tok.split(".")[0]))["kid"] == api_auth.key_id(NEW))

print("\n[1b] token_version still enforced through the accept-list path")
api_auth.logout_all_devices("42")
check("revoked token fails on every key", api_auth.verify_session_token(tok, [NEW, OLD]) is None)


# ── [2] Device-cache fail-open ───────────────────────────────────────────────

print("\n[2] Device gate: Firestore outage must not grant permanent trust")
sessions.docs["7"] = {"allowed_devices": ["realdev"], "token_version": 0}
api_auth._allowed_devices.clear()

sessions.fail = True
got = api_auth._get_allowed_devices("7")
check("outage + no cache -> None (unknowable), not empty set", got is None)
check("the guess was NOT cached", "7" not in api_auth._allowed_devices)
verdict = api_auth.check_device("7", "attacker-device")
check("outage: request passes (fail open, availability)", verdict == "ok")
sessions.fail = False
check("but the attacker device was NOT adopted",
      "attacker-device" not in sessions.docs["7"]["allowed_devices"])
check("healthy read again blocks the unknown device",
      api_auth.check_device("7", "attacker-device") == "unknown")
check("and still passes the real device", api_auth.check_device("7", "realdev") == "ok")

print("\n[2b] outage with a cached value serves the stale truth")
sessions.fail = True
check("cached set survives an outage read",
      api_auth.check_device("7", "realdev") == "ok"
      and api_auth.check_device("7", "attacker-device") == "unknown")
sessions.fail = False

print("\n[2c] a healthy empty account requires approval — no silent trust-on-first-use")
sessions.docs["8"] = {"token_version": 0}
api_auth._allowed_devices.clear()
# A legacy account with no bound device must NOT adopt the first id it sees —
# a stolen token would otherwise bind the thief's device with no approval. It
# reads as unknown, which drives the DM-approval flow; nothing is written yet.
check("first device NOT auto-adopted", api_auth.check_device("8", "firstdev") == "unknown")
check("nothing persisted without approval",
      "firstdev" not in (sessions.docs["8"].get("allowed_devices") or []))
# Approval (the DM the owner receives and accepts) is what binds it; the
# device-approval path calls add_allowed_device, modelled here directly.
api_auth.add_allowed_device("8", "firstdev")
check("approved device now trusted", api_auth.check_device("8", "firstdev") == "ok")
check("approval recorded metadata",
      "firstdev" in sessions.docs["8"].get("device_meta", {}))
check("a different device is still blocked", api_auth.check_device("8", "seconddev") == "unknown")


# ── [3] Device removal / listing ─────────────────────────────────────────────

print("\n[3] Trusted-device management")
api_auth.add_allowed_device("8", "laptopdev")
listing = api_auth.list_devices("8")
ids = [d["device_id"] for d in listing]
check("list shows both devices", ids == ["firstdev", "laptopdev"])
check("list carries added_at for metadata-era devices",
      all(d["added_at"] for d in listing))
check("removing an unknown id is a no-op False",
      api_auth.remove_allowed_device("8", "nosuch") is False)
check("removing a real id returns True", api_auth.remove_allowed_device("8", "laptopdev") is True)
check("removal persisted", "laptopdev" not in sessions.docs["8"]["allowed_devices"])
check("metadata cleaned up", "laptopdev" not in sessions.docs["8"].get("device_meta", {}))
check("removal is effective immediately (cache)",
      api_auth.check_device("8", "laptopdev") == "unknown")
check("remaining device still trusted", api_auth.check_device("8", "firstdev") == "ok")
api_auth.remove_allowed_device("8", "firstdev")
check("removing the last device does NOT re-arm silent adoption",
      api_auth.check_device("8", "newpc") == "unknown"
      and "newpc" not in (sessions.docs["8"].get("allowed_devices") or []))


# ── [4] Link-code sweep defense ──────────────────────────────────────────────

print("\n[4] Wrong link codes lock out the address that sent them")
import types
import api_server

api_server._LINK_FAILURES.clear()
api_server._RATE_BUCKETS.clear()
req = lambda ip: types.SimpleNamespace(client=types.SimpleNamespace(host=ip), headers={})
for _ in range(api_server._LINK_FAIL_MAX - 1):
    api_server._note_failed_link_guess("10.9.9.9")
api_server._guard_link_attempt(req("10.9.9.9"))
check("below threshold: the address may still try", True)
api_server._note_failed_link_guess("10.9.9.9")
try:
    api_server._guard_link_attempt(req("10.9.9.9"))
    locked = False
except Exception as exc:
    locked = getattr(exc, "status_code", None) == 429
check("at threshold: the address is refused (429)", locked)
api_server._guard_link_attempt(req("10.9.9.8"))
check("another address is unaffected", True)
check("no global purge exists any more", not hasattr(api_server, "_FAILED_LINK_GUESSES"))

print("\n[4b] the global link-code purge is gone (dead since the per-IP lockout)")
check("no purge_all_link_codes", not hasattr(api_auth, "purge_all_link_codes"))


# ── [5] Source guards ────────────────────────────────────────────────────────

print("\n[5] Source guards (wiring can't silently regress)")
BOT = os.path.dirname(os.path.abspath(__file__))
api = open(os.path.join(BOT, "api_server.py"), encoding="utf-8").read()
bridge = open(os.path.join(BOT, "cogs", "ksp_bridge.py"), encoding="utf-8").read()
conf = open(os.path.join(BOT, "config.py"), encoding="utf-8").read()

check("no required Header(...) left on authorization (401 not 422)",
      'authorization: str = Header(...)' not in api)
check("every verify call site uses _accept_secrets()",
      len(re.findall(r"verify_session_token\([^)]*_accept_secrets\(\)", api)) >= 1
      and "verify_session_token(authorization[7:], _get_api_secret())" not in api
      and "verify_session_token(token, _get_api_secret())" not in api)
check("signing still uses the current key only",
      "_get_api_secret(),\n" in api or "_get_api_secret())" in api)
check("WS accepts tickets only (no ?token= fallback that logs the session token)",
      'websocket.query_params.get("token"' not in api
      and 'websocket.query_params.get("ticket"' in api)
check("device report is ownership-checked",
      'target.get("user_id")) != str(user.get("user_id")' in api)
check("device management endpoints exist behind get_current_user",
      '"/api/v1/auth/devices"' in api and '"/api/v1/auth/devices/remove"' in api
      and re.search(r'auth_devices_list\(.*?get_current_user', api, re.S) is not None)
check("ban listener revokes sessions in the linked guild",
      "on_member_ban" in bridge and "get_linked_guild" in bridge
      and "logout_all_devices" in bridge)
check("config refuses placeholder/duplicate previous key",
      "API_SECRET_KEY_PREVIOUS" in conf
      and 'cfg.API_SECRET_KEY_PREVIOUS = ""' in conf)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
