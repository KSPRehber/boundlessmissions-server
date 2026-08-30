"""Offline exercise of the ticket record: threads, the echo guard, and ownership.

A ticket used to BE a Discord channel, so none of this could be tested at all —
there was nothing but a channel and a counter. Now the record is the ticket, and
these are the two things that make it work as a two-way conversation:

  • a message posted to the website and mirrored into Discord must not come back
    through the listener as a second copy, and
  • one player must never be able to read or answer another player's ticket.

Firestore is a dict-backed fake, including the subcollection and the increment.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

import api_server
import api_auth
from data import tickets as tdb
from data import accounts as acc

# ── a fake Firestore, with subcollections and Increment ──────────────────────

DATA: dict[str, dict] = {}


class _Inc:
    def __init__(self, n): self.n = n


class _Snap:
    def __init__(self, path):
        self._path = path
        self.exists = path in DATA
        self.id = path.rsplit("/", 1)[1]
        self.reference = _Doc(path)

    def to_dict(self):
        return dict(DATA.get(self._path, {}))


class _Query:
    def __init__(self, prefix, field, value):
        self._prefix, self._field, self._value = prefix, field, value

    def limit(self, _n): return self

    def stream(self):
        for path, payload in list(DATA.items()):
            head, _, tail = path.rpartition("/")
            if head == self._prefix and payload.get(self._field) == self._value:
                yield _Snap(path)


class _Doc:
    def __init__(self, path): self._path = path

    def get(self, transaction=None): return _Snap(self._path)

    def set(self, payload, merge=False):
        if merge:
            cur = DATA.setdefault(self._path, {})
            for k, v in payload.items():
                cur[k] = (int(cur.get(k, 0) or 0) + v.n) if isinstance(v, _Inc) else v
        else:
            DATA[self._path] = {k: (v.n if isinstance(v, _Inc) else v)
                                for k, v in payload.items()}

    def delete(self): DATA.pop(self._path, None)

    def collection(self, name): return _Col(f"{self._path}/{name}")


class _Col:
    def __init__(self, name): self._name = name

    def document(self, doc_id): return _Doc(f"{self._name}/{doc_id}")

    def where(self, field=None, op=None, value=None, filter=None):
        if filter is not None:
            field, value = filter.field_path, filter.value
        return _Query(self._name, field, value)

    def stream(self):
        for path in list(DATA):
            head, _, _t = path.rpartition("/")
            if head == self._name:
                yield _Snap(path)


class _DB:
    def collection(self, name): return _Col(name)


tdb._db = _DB()
tdb.firestore = type("_FS", (), {"Increment": staticmethod(_Inc)})()

# ── wire the API to the same fake ────────────────────────────────────────────

ACCOUNTS: dict[str, dict] = {}
api_server.tdb = tdb
api_server.accounts.get_account = lambda aid: ACCOUNTS.get(str(aid))
api_server.accounts.is_discord_account = acc.is_discord_account

SECRET = "t" * 48
api_server._get_api_secret = lambda: SECRET
api_server.verify_session_token = lambda tok, sec: api_auth.verify_session_token(tok, SECRET)
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

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def auth(aid):
    return {"Authorization": f"Bearer {api_auth.create_session_token('0', str(aid), 'T', SECRET, aud=api_auth.AUD_WEB)}"}


def reset():
    DATA.clear()
    ACCOUNTS.clear()
    if hasattr(api_server, "_rate_buckets"):
        api_server._rate_buckets.clear()


def main():
    ME = "a_playerone000000000000001"
    THEM = "a_playertwo000000000000002"

    print("\nthe record is the ticket")
    reset()
    t = tdb.create(guild_id="999", opener_id=ME, kind="bug", title="Struts explode",
                   description="Every time.", number=7, channel_id="555")
    check("creates it", t and t["status"] == tdb.OPEN, t)
    check("keyed by an account id, not a Discord id", t["opener_id"] == ME)
    check("findable by its channel — how the listener knows",
          (tdb.get_by_channel("555") or {}).get("ticket_id") == t["ticket_id"])
    check("a channel that is not a ticket resolves to nothing",
          tdb.get_by_channel("111") is None)

    print("\na ticket with no channel is still a ticket")
    reset()
    t2 = tdb.create(guild_id="999", opener_id=ME, kind="other", title="No channel",
                    number=8)
    check("no channel_id required", t2 and t2["channel_id"] == "")
    check("and it still lists", len(tdb.list_for_account(ME)) == 1)

    print("\nthreads")
    reset()
    t = tdb.create(guild_id="999", opener_id=ME, kind="other", title="Hello", number=1,
                   channel_id="555")
    tid = t["ticket_id"]
    tdb.add_message(tid, author_id=ME, author_name="Me", author_kind=tdb.AUTHOR_OPENER,
                    body="first")
    tdb.add_message(tid, author_id="42", author_name="Mod", author_kind=tdb.AUTHOR_STAFF,
                    body="second", discord_message_id="m2")
    msgs = tdb.messages(tid)
    check("both messages are there", len(msgs) == 2, msgs)
    check("oldest first", msgs[0]["body"] == "first")
    check("the counter tracks them", tdb.get(tid)["message_count"] == 2)
    check("a staff reply raises the unread flag",
          tdb.get(tid)["unread_for_opener"] is True)
    tdb.mark_read(tid)
    check("reading clears it", tdb.get(tid)["unread_for_opener"] is False)

    tdb.add_message(tid, author_id=ME, author_name="Me", author_kind=tdb.AUTHOR_OPENER,
                    body="third")
    check("the opener's own reply does NOT mark it unread for them",
          tdb.get(tid)["unread_for_opener"] is False)

    print("\nthe echo guard")
    check("a mirrored Discord message is recognised",
          tdb.has_discord_message(tid, "m2"))
    check("an unseen one is not", not tdb.has_discord_message(tid, "m999"))
    check("and an empty id is never a match", not tdb.has_discord_message(tid, ""))

    print("\nthe API is scoped to the owner")
    reset()
    ACCOUNTS[ME] = {"account_id": ME, "username": "one", "display_name": "One"}
    ACCOUNTS[THEM] = {"account_id": THEM, "username": "two", "display_name": "Two"}
    mine = tdb.create(guild_id="999", opener_id=ME, kind="other", title="Mine",
                      number=1, channel_id="555")
    tid = mine["ticket_id"]

    r = client.get("/api/v1/web/tickets", headers=auth(ME))
    check("I see my ticket", r.status_code == 200 and len(r.json()["tickets"]) == 1, r.text)
    r = client.get("/api/v1/web/tickets", headers=auth(THEM))
    check("they see none of mine", r.status_code == 200 and r.json()["tickets"] == [], r.text)

    r = client.get(f"/api/v1/web/tickets/{tid}", headers=auth(ME))
    check("I can read the thread", r.status_code == 200, r.text)
    r = client.get(f"/api/v1/web/tickets/{tid}", headers=auth(THEM))
    check("they cannot — and get 404, not 403, so the id is not confirmed",
          r.status_code == 404, r.status_code)
    r = client.post(f"/api/v1/web/tickets/{tid}/reply", json={"body": "hi"},
                    headers=auth(THEM))
    check("nor reply to it", r.status_code == 404, r.status_code)
    check("nothing was written", tdb.get(tid)["message_count"] == 0)

    print("\nreplying")
    r = client.post(f"/api/v1/web/tickets/{tid}/reply", json={"body": "please help"},
                    headers=auth(ME))
    check("goes through", r.status_code == 200, r.text)
    check("and lands in the thread", tdb.get(tid)["message_count"] == 1)
    check("attributed to the opener",
          tdb.messages(tid)[0]["author_kind"] == tdb.AUTHOR_OPENER)

    print("\nreading clears the badge")
    tdb.add_message(tid, author_id="42", author_name="Mod",
                    author_kind=tdb.AUTHOR_STAFF, body="on it")
    r = client.get("/api/v1/web/tickets", headers=auth(ME))
    check("the list shows unread", r.json()["tickets"][0]["unread"] is True)
    client.get(f"/api/v1/web/tickets/{tid}", headers=auth(ME))
    r = client.get("/api/v1/web/tickets", headers=auth(ME))
    check("opening it clears the badge", r.json()["tickets"][0]["unread"] is False)

    print("\na closed ticket is read-only")
    tdb.close(tid, "42")
    r = client.post(f"/api/v1/web/tickets/{tid}/reply", json={"body": "one more"},
                    headers=auth(ME))
    check("replies are refused", r.status_code == 409, r.status_code)
    r = client.get(f"/api/v1/web/tickets/{tid}", headers=auth(ME))
    check("but it can still be read", r.status_code == 200
          and r.json()["ticket"]["status"] == tdb.CLOSED, r.text)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())
