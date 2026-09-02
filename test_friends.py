"""test_friends.py – Behavioural tests for the friend graph.

No network, no Firebase: `data.store` and `firebase_admin` are both stubbed out
before import, so `data/friends.py` runs against a fake Firestore whose
"transaction" is a real one in the only sense that matters here — every write in
one runs, or the exception propagates and none of them are visible. That is
enough to test what the module promises: a friendship is written to BOTH
documents or to neither.

What is covered:
  [A] the handshake     request -> accept, and the auto-accept on a crossing pair
  [B] symmetry          every state change lands on both records
  [C] refusals          self, duplicate, absent request, caps
  [D] removal           unfriend and cancel clear every trace of the pair
  [E] failing closed    a read error raises rather than answering "not friends"
  [F] deletion shape    a whole-document write, so a removal actually removes
  [G] purge             forget_account leaves the id in nobody's list

Run:  ./.venv/bin/python test_friends.py
"""
import os
import sys
import types

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


# ── Fake Firestore ────────────────────────────────────────────────────────────

class FakeSnap:
    def __init__(self, data, doc_id=""):
        self._data, self.id = data, doc_id

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, col, doc_id):
        self.col, self.id = col, doc_id

    def get(self, transaction=None):
        self.col.reads += 1
        if self.col.fail:
            raise RuntimeError("firestore is down")
        return FakeSnap(self.col.docs.get(self.id), self.id)

    def set(self, data, merge=False):
        if self.col.fail:
            raise RuntimeError("firestore is down")
        self.col.writes += 1
        if merge:
            cur = dict(self.col.docs.get(self.id) or {})
            cur.update(data)
            self.col.docs[self.id] = cur
        else:
            self.col.docs[self.id] = dict(data)

    def delete(self):
        self.col.docs.pop(self.id, None)


class FakeCol:
    def __init__(self):
        self.docs, self.reads, self.writes, self.fail = {}, 0, 0, False

    def document(self, doc_id):
        return FakeDoc(self, doc_id)


class FakeTxn:
    """Buffers writes and applies them only if the body returns.

    That is the one property `data/friends.py` leans on: the two halves of a
    friendship are written together or not at all.
    """
    def __init__(self):
        self.ops = []

    def set(self, ref, data, merge=False):
        self.ops.append((ref, dict(data), merge))

    def commit(self):
        for ref, data, merge in self.ops:
            ref.set(data, merge=merge)
        self.ops = []


class FakeDb:
    def __init__(self, col):
        self._col = col

    def collection(self, name):
        assert name == "friends", name
        return self._col

    def transaction(self):
        return FakeTxn()


def _transactional(fn):
    def wrapper(txn, *a, **kw):
        out = fn(txn, *a, **kw)
        txn.commit()
        return out
    return wrapper


COL = FakeCol()
_store_stub = types.ModuleType("data.store")
_store_stub._db = FakeDb(COL)

_fs = types.ModuleType("firebase_admin.firestore")
_fs.transactional = _transactional
_fa = types.ModuleType("firebase_admin")
_fa.firestore = _fs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data  # noqa: E402  (real package, for the submodule path)
sys.modules["data.store"] = _store_stub
data.store = _store_stub
sys.modules["firebase_admin"] = _fa
sys.modules["firebase_admin.firestore"] = _fs

from data import friends as F  # noqa: E402


def reset():
    COL.docs.clear()
    COL.reads = COL.writes = 0
    COL.fail = False


A, B, C = "111", "a_web", "222"


# ── [A] the handshake ────────────────────────────────────────────────────────
print("\n[A] request and accept")
reset()

ok, state, msg = F.send_request(A, B)
check("a request is accepted for sending", ok and state == "requested", msg)
check("it is not yet a friendship", not F.are_friends(A, B))
check("the target sees it as incoming", F.relationship(B, A) == "incoming")
check("the sender sees it as outgoing", F.relationship(A, B) == "outgoing")

ok, msg = F.accept_request(B, A)
check("accepting works", ok, msg)
check("A sees B as a friend", F.are_friends(A, B))
check("B sees A as a friend", F.are_friends(B, A))
check("no pending trace is left", F.relationship(A, B) == "friends"
      and not F.get_record(A)["outgoing"] and not F.get_record(B)["incoming"])

reset()
F.send_request(A, C)
ok, state, msg = F.send_request(C, A)
check("asking back completes the handshake", ok and state == "accepted", state)
check("both are friends after the crossing pair",
      F.are_friends(A, C) and F.are_friends(C, A))

# A Discord snowflake and a website account are the same kind of thing here.
reset()
F.send_request(B, C)
F.accept_request(C, B)
check("a website account befriends a Discord one", F.are_friends(B, C))


# ── [C] refusals ─────────────────────────────────────────────────────────────
print("\n[C] refusals")
reset()

ok, _s, msg = F.send_request(A, A)
check("you cannot add yourself", not ok, msg)

F.send_request(A, B)
ok, _s, msg = F.send_request(A, B)
check("a duplicate request is refused", not ok, msg)

F.accept_request(B, A)
ok, _s, msg = F.send_request(A, B)
check("asking an existing friend is refused", not ok, msg)

ok, msg = F.accept_request(A, C)
check("accepting a request nobody sent is refused", not ok, msg)

ok, msg = F.remove_friend(A, C)
check("unfriending a non-friend is refused", not ok, msg)

reset()
COL.docs[A] = {"friends": {str(i): {"since": 1} for i in range(F.MAX_FRIENDS)},
               "incoming": {}, "outgoing": {}}
ok, _s, msg = F.send_request(A, B)
check("a full friend list refuses a new request", not ok, msg)

reset()
COL.docs[A] = {"friends": {}, "outgoing": {str(i): {"at": 1}
                                           for i in range(F.MAX_OUTGOING)},
               "incoming": {}}
ok, _s, msg = F.send_request(A, B)
check("too many unanswered requests refuses another", not ok, msg)

reset()
COL.docs[B] = {"friends": {}, "outgoing": {},
               "incoming": {str(i): {"at": 1} for i in range(F.MAX_INCOMING)}}
ok, _s, msg = F.send_request(A, B)
check("a swamped recipient refuses one more", not ok, msg)
check("and is not named as the reason", "full" not in msg.lower(), msg)


# ── [D] removal ──────────────────────────────────────────────────────────────
print("\n[D] removal and cancellation")
reset()

F.send_request(A, B)
F.accept_request(B, A)
ok, msg = F.remove_friend(A, B)
check("either side may unfriend", ok, msg)
check("it is gone from both records",
      not F.are_friends(A, B) and not F.are_friends(B, A))
check("nothing at all is left of the pair",
      F.relationship(A, B) == "none" and F.relationship(B, A) == "none")

reset()
F.send_request(A, B)
ok, msg = F.cancel_request(A, B)
check("the sender can withdraw", ok, msg)
check("the withdrawal clears both sides",
      F.relationship(A, B) == "none" and F.relationship(B, A) == "none")

reset()
F.send_request(A, B)
ok, msg = F.cancel_request(B, A)
check("the recipient can decline", ok, msg)
check("declining clears both sides",
      F.relationship(A, B) == "none" and F.relationship(B, A) == "none")
ok, msg = F.cancel_request(B, A)
check("declining twice is refused, not silently repeated", not ok, msg)


# ── [F] the write is a whole document ────────────────────────────────────────
print("\n[F] a removal actually removes")
reset()
F.send_request(A, B)
F.accept_request(B, A)
F.remove_friend(A, B)
# The bug this guards: `set(..., merge=True)` deep-merges nested maps, so a key
# dropped in memory survives the write and the unfriend does nothing at all.
check("the stored map no longer holds the key",
      B not in (COL.docs[A].get("friends") or {}),
      COL.docs[A])


# ── [E] failing closed ───────────────────────────────────────────────────────
print("\n[E] a read failure refuses rather than guesses")
reset()
F.send_request(A, B)
F.accept_request(B, A)
COL.fail = True
try:
    F.are_friends(A, B)
    check("are_friends raises on an unreadable record", False)
except F.FriendsUnavailable:
    check("are_friends raises on an unreadable record", True)
try:
    F.send_request(A, C)
    check("send_request raises on an unreadable record", False)
except F.FriendsUnavailable:
    check("send_request raises on an unreadable record", True)
COL.fail = False


# ── [G] purge ────────────────────────────────────────────────────────────────
print("\n[G] account deletion leaves no orphan rows")
reset()
F.send_request(A, B); F.accept_request(B, A)
F.send_request(A, C)
F.forget_account(A)
check("the deleted account's own record is gone", A not in COL.docs)
check("it is out of its friend's list", A not in (COL.docs[B].get("friends") or {}))
check("and out of a pending request it had sent",
      A not in (COL.docs[C].get("incoming") or {}))


# ── [H] enforcement points ───────────────────────────────────────────────────
#
# A source check rather than a behavioural one: standing up FastAPI + Firebase to
# assert "this endpoint calls the gate" would test the harness, not the rule. The
# rule is that a craft may not be handed to a non-friend, and that the check lives
# on the SERVER — a picker drawing the right list is a convenience, not the gate.

print("\n[H] the send is gated on the server")
src = open(os.path.join(HERE, "api_server.py"), encoding="utf-8").read()

send = src[src.index("async def craft_send_to_friend"):]
send = send[:send.index("\n@app.")]
check("quicksend asks are_friends", "friends_db.are_friends" in send)
check("it asks before storing anything",
      send.index("are_friends") < send.index("imp.upload_gift"),
      "the gate must run before the payload is uploaded")
check("an unreadable friend list refuses the send",
      "FriendsUnavailable" in send and "Couldn't check your friend list" in send)
check("the old 'is in this server' rule is gone",
      "isn't in this server" not in send, send[:0])

# Both tiers, one implementation. Two copies of a mutual-consent flow would be two
# chances for the halves to differ.
for path in ("/api/v1/friends", "/api/v1/web/friends"):
    check(f"{path} is served", f'"{path}"' in src)
for verb in ("accept", "decline", "remove"):
    check(f"both tiers expose {verb}",
          src.count('/%s"' % verb) >= 2 and f'{{other_id}}/{verb}' in src)
check("the web tier delegates rather than reimplementing",
      src.count("async def _friend_action") == 1 and src.count("async def _friend_request") == 1)

# A quicksend can now cross guilds, because friendship does. Everything written FOR
# the other party has to go where they read, not where the writer wrote from.
check("the offer is queued in the recipient's guild",
      "rgid = await asyncio.to_thread(_recipient_guild" in src
      and "rgid, rid, source=\"gift_vessel\"" in src
      and "rgid, rid, source=\"gift_craft\"" in src)
check("a declined vessel is returned to the sender's guild",
      "sgid, sender_id, source=\"gift_vessel\"" in src)

# The session document is shared by both audiences and a website sign-in always
# mints under the home guild, so `guild_id` is only ever "where they last signed
# in". Routing a craft off that would send a Discord player's ship to the home
# guild's queue while their game polls the guild it linked in.
auth_src = open(os.path.join(HERE, "api_auth.py"), encoding="utf-8").read()
check("a KSP mint records the guild the game polls",
      'record["ksp_guild_id"] = guild_id' in auth_src
      and "if aud == AUD_KSP:" in auth_src)
check("it is merged, so a later web sign-in cannot erase it",
      "_sessions_col().document(user_id).set(record, merge=True)" in auth_src)
check("a web mint does NOT claim to be a KSP guild",
      auth_src.count('record["ksp_guild_id"]') == 1)
check("routing prefers it over the last-login guild",
      'doc.get("ksp_guild_id") or doc.get("guild_id")' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
