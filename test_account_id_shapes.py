"""A non-snowflake account id must survive every endpoint a signed-in browser hits.

This exists because of a real bug. Phase 2 gave website sign-ups an account id of
`a_<firebase uid>`, and phase 3 started minting session tokens carrying one — but
the endpoints still did `int(user["user_id"])`, a coercion left over from when
every id was a Discord snowflake. So sign-in worked, the cookie was set, and then
`/web/profile` raised ValueError; the browser read the 500 as "signed out" and the
whole thing looked like the button did nothing.

Nothing here asserts a *value*. The only claim is that none of these 500 — because
the failure mode is a type error deep in a handler, and any 4xx (not signed up, no
listings, no such contract) is a real answer that means the id got through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

import api_server
import api_auth
from data import accounts as acc

SECRET = "z" * 48
api_server._get_api_secret = lambda: SECRET
api_server.verify_session_token = lambda t, s: api_auth.verify_session_token(t, SECRET)
api_server.enforce_not_suspended = lambda *a, **k: None
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

def src_of(name: str) -> str:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
              encoding="utf-8") as fh:
        return fh.read()


FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def headers_for(account_id):
    tok = api_auth.create_session_token("0", str(account_id), "Jeb", SECRET)
    return {"Authorization": f"Bearer {tok}"}


# Every GET a signed-in account page / marketplace session makes.
READS = [
    ("profile", "/api/v1/web/profile"),
    ("my uploads", "/api/v1/web/marketplace/mine"),
    ("my purchases", "/api/v1/web/marketplace/purchases"),
    ("my votes", "/api/v1/web/marketplace/votes"),
    ("my contracts", "/api/v1/web/contracts"),
    ("auctions", "/api/v1/web/auctions"),
]


def main():
    web_id = acc.firebase_account_id("FirebaseUid00000000000000001")
    discord_id = "123456789012345678"

    print(f"\nweb-only account ({web_id})")
    h = headers_for(web_id)
    for label, path in READS:
        r = client.get(path, headers=h)
        check(f"{label} does not blow up", r.status_code < 500,
              f"HTTP {r.status_code} {r.text[:140]}")

    print(f"\nDiscord account ({discord_id}) — the shape that always worked")
    h = headers_for(discord_id)
    for label, path in READS:
        r = client.get(path, headers=h)
        check(f"{label} still fine", r.status_code < 500,
              f"HTTP {r.status_code} {r.text[:140]}")

    print("\nthe coercion that caused it is gone")
    check('no int(user["user_id"]) anywhere in api_server',
          'int(user["user_id"])' not in src_of("api_server.py"))

    print("\nself-action guards compare like with like")
    # These three all read `<my id> == <their id>`, and all three silently stopped
    # working when account ids became strings while the other side stayed an int:
    # `str == int` is never true, so every guard passed everyone. A dead guard
    # raises no error and writes no log — the only way it surfaces is somebody
    # contracting themselves — so the shapes are asserted here rather than trusted.
    api_src = src_of("api_server.py")
    for label, snippet in [
        ("contract: contractor id stays a string",
         'contractor_id = str(req.contractor_id).strip()'),
        ("rescue: contractor id stays a string",
         'contractor_uid = str(contractor_id).strip()'),
        ("quicksend: recipient id stays a string",
         'rid = str(recipient_id).strip()'),
        ("marketplace buy: seller id stays a string",
         'seller_id = str(listing["seller_id"])'),
    ]:
        check(label, snippet in api_src)

    for label, gone in [
        ("no int() on the contractor id", 'contractor_id = int(req.contractor_id)'),
        ("no int() on the recipient id", 'rid = int(recipient_id)'),
        ("no int() on the seller id", 'seller_id = int(listing["seller_id"])'),
    ]:
        check(label, gone not in api_src)

    print("\nthe player picker reaches guild-less accounts")
    # A website account has no guild of its own, so its corp is filed under the
    # home guild. Scoping the picker to the caller's guild made those players
    # invisible to anyone who linked through a different server — they could hold
    # an account, a balance and a username, and simply never be hireable.
    api_src = src_of("api_server.py")
    check("list_corps also scans the home guild",
          "cfg.HOME_GUILD_ID" in api_src and "_corps_col(home)" in api_src)
    check("but only its guild-less corps, so guild visibility does not widen",
          '.get("web_only")' in api_src)
    check("and the merge de-dupes, since one player may have a corp in both",
          "if doc.id in seen:" in api_src)

    check("a website account's own picture is used when there is no Discord one",
          'if avatar_url is None and d.get("avatar_url")' in api_src)
    check("and its name/picture are re-synced when they change, since a corp with "
          "no Discord member shows what is STORED",
          api_src.count("sync_web_corp_profile") >= 3, api_src.count("sync_web_corp_profile"))

    check("a corp is created when onboarding completes, not only on a KSP link",
          "ensure_corp_record_for_account" in api_src
          and api_src.count("ensure_corp_record_for_account") >= 4,
          api_src.count("ensure_corp_record_for_account"))

    print("\nid helpers tell the two apart")
    check("a snowflake resolves to a Discord id",
          api_server._discord_id(discord_id) == int(discord_id))
    check("a web account resolves to none",
          api_server._discord_id(web_id) is None)
    check("a mention is a mention when there is someone to mention",
          api_server._mention(discord_id) == f"<@{discord_id}>")
    check("and plain text when there is not — <@a_…> renders as broken text",
          api_server._mention(web_id, "no Discord") == "no Discord")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())
