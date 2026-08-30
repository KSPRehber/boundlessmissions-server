"""Offline exercise of the moderator target resolver (cogs/targets.py).

The thing under test is not "does a username look up" — it is the set of refusals
around it, because every one of them is a case where guessing would send a
moderator's action to the wrong wallet or send them chasing a name that was
correct all along:

  • a Discord member whose snowflake is bound to a *different* account resolves to
    that account, not to the snowflake;
  • a failed Firestore read is refused, never read as "no such player";
  • both fields filled is refused rather than resolved;
  • an empty target is refused on a command that writes, and means "me" only on
    the ones that read;
  • a typo'd account id is refused rather than minting an empty wallet.

Firestore is the same dict-backed fake `test_accounts.py` uses, extended with the
document-id range query `search_usernames` needs. Discord is a handful of stubs:
none of this touches a gateway.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data import accounts as acc

# ── a fake Firestore ─────────────────────────────────────────────────────────

DATA: dict[str, dict] = {}
FAIL_READS: set[str] = set()


class _Snap:
    def __init__(self, path):
        self._path = path
        self.exists = path in DATA

    def to_dict(self):
        return dict(DATA.get(self._path, {}))


class _Doc:
    def __init__(self, path):
        self._path = path

    def get(self, transaction=None):
        if self._path.split("/")[0] in FAIL_READS:
            raise RuntimeError("firestore down")
        return _Snap(self._path)

    def set(self, payload, merge=False):
        if merge:
            DATA.setdefault(self._path, {}).update(payload)
        else:
            DATA[self._path] = dict(payload)

    def delete(self):
        DATA.pop(self._path, None)


class _QueryDoc:
    def __init__(self, path):
        self.id = path.split("/", 1)[1]

    def to_dict(self):
        return dict(DATA.get(self.id, {}))


class _RangeQuery:
    """Just enough of an ordered document-id query for `search_usernames`."""

    def __init__(self, name):
        self._name, self._lo, self._hi, self._limit = name, "", None, 1000

    def order_by(self, _field):
        return self

    def start_at(self, cursor):
        self._lo = cursor["__name__"]
        return self

    def end_at(self, cursor):
        self._hi = cursor["__name__"]
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        if self._name in FAIL_READS:
            raise RuntimeError("firestore down")
        ids = sorted(p.split("/", 1)[1] for p in DATA
                     if p.startswith(self._name + "/"))
        out = [i for i in ids if i >= self._lo and (self._hi is None or i <= self._hi)]
        for doc_id in out[: self._limit]:
            yield _QueryDoc(f"{self._name}/{doc_id}")


class _Query:
    def __init__(self, name, field, value):
        self._name, self._field, self._value = name, field, value

    def limit(self, _n):
        return self

    def stream(self):
        for path, payload in list(DATA.items()):
            if path.startswith(self._name + "/") and payload.get(self._field) == self._value:
                yield _QueryDoc(path)


class _Col:
    def __init__(self, name):
        self._name = name

    def document(self, doc_id):
        return _Doc(f"{self._name}/{doc_id}")

    def where(self, field=None, op=None, value=None, filter=None):
        if filter is not None:
            field, value = filter.field_path, filter.value
        return _Query(self._name, field, value)

    def order_by(self, field):
        return _RangeQuery(self._name).order_by(field)


class _Txn:
    def set(self, ref, payload, merge=False):
        ref.set(payload, merge=merge)

    def update(self, ref, payload):
        ref.set(payload, merge=True)


class _DB:
    def collection(self, name):
        return _Col(name)

    def transaction(self):
        return _Txn()


acc._db = _DB()
acc.firestore = type("_FS", (), {"transactional": staticmethod(lambda fn: fn)})()

# ── Discord + store stubs ────────────────────────────────────────────────────

import cogs.targets as tg          # noqa: E402  (must follow the _db swap)


class _Member:
    def __init__(self, mid, name):
        self.id = int(mid)
        self.display_name = name
        self.mention = f"<@{mid}>"
        self.sent = []

    async def send(self, *a, **kw):
        self.sent.append((a, kw))


class _Guild:
    def __init__(self, members=()):
        self._members = {m.id: m for m in members}

    def get_member(self, mid):
        return self._members.get(int(mid))


class _Interaction:
    def __init__(self, guild=None, user=None):
        self.guild = guild
        self.user = user


class _Store:
    def __init__(self):
        self.users = {}

    def has_user(self, uid):
        return str(uid) in self.users


FAKE_STORE = _Store()
tg.store = FAKE_STORE

# ── harness ──────────────────────────────────────────────────────────────────

FAILED: list[str] = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILED.append(label)


def reset():
    DATA.clear()
    FAIL_READS.clear()
    FAKE_STORE.users.clear()
    tg._cache.clear()
    tg._names.clear()


def run(coro):
    return asyncio.run(coro)


async def resolve_err(interaction, member, username, **kw):
    """(target, error_text) — the shape every call site actually branches on."""
    try:
        return await tg.resolve(interaction, member, username, **kw), ""
    except tg.TargetError as err:
        return None, str(err)


DISCORD = "1902000000000000"
OTHER = "1902999999999999"
FBUID = "firebase-abc"


def main() -> int:
    print("owner_of_username — free vs unreadable")
    reset()
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "orbital-dave")
    check("a claimed name resolves", acc.owner_of_username("Orbital-Dave") == web)
    check("an unclaimed name is \"\", not None", acc.owner_of_username("nobody") == "")
    FAIL_READS.add("usernames")
    check("an unreadable name is None, not \"\"", acc.owner_of_username("orbital-dave") is None)
    FAIL_READS.clear()
    check("account_for_username still conflates the two (unchanged)",
          acc.account_for_username("nobody") is None)

    print("\nsearch_usernames — prefix scan")
    reset()
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "orbital-dave")
    acc.ensure_firebase_account("fb2")
    acc.claim_username(acc.firebase_account_id("fb2"), "orbiter")
    acc.ensure_firebase_account("fb3")
    acc.claim_username(acc.firebase_account_id("fb3"), "zenith")
    check("prefix matches both", acc.search_usernames("orb") == ["orbital-dave", "orbiter"])
    check("prefix is case-insensitive", acc.search_usernames("ORB") == ["orbital-dave", "orbiter"])
    check("empty prefix lists all", len(acc.search_usernames("")) == 3)
    check("no match is empty", acc.search_usernames("qq") == [])
    check("limit is honoured", len(acc.search_usernames("", 1)) == 1)
    FAIL_READS.add("usernames")
    check("a failed scan is empty, never an exception", acc.search_usernames("orb") == [])
    FAIL_READS.clear()

    print("\nresolve — a website-only account, by username")
    reset()
    acc.ensure_firebase_account(FBUID, email="dave@example.com")
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "orbital-dave")
    acc.set_display_name(web, "Orbital Dave")
    it = _Interaction(guild=_Guild(), user=_Member(DISCORD, "mod"))
    tgt, err = run(resolve_err(it, None, "orbital-dave"))
    check("resolves to the web account id", tgt is not None and tgt.account_id == web)
    check("label names them the way the site does",
          tgt is not None and tgt.label == "Orbital Dave (@orbital-dave)")
    check("no member, so no mention and no DM",
          tgt is not None and tgt.member is None and not tgt.can_dm
          and tgt.mention == "**Orbital Dave (@orbital-dave)**")
    check("a DM to them is refused, not raised", run(tgt.dm("hi")) is False)
    check("case does not matter",
          run(resolve_err(it, None, "ORBITAL-DAVE"))[0].account_id == web)

    print("\nresolve — a Discord member")
    reset()
    member = _Member(DISCORD, "Jeb")
    it = _Interaction(guild=_Guild([member]), user=member)
    acc.ensure_discord_account(DISCORD, "jeb")
    tgt, err = run(resolve_err(it, member, None))
    check("a plain member is their own account", tgt is not None and tgt.account_id == DISCORD)
    check("member is carried through, so DMs work",
          tgt is not None and tgt.member is member and tgt.can_dm)
    check("a DM is delivered", run(tgt.dm("hi")) is True and member.sent)

    print("\nresolve — a rebound snowflake (the bug this fixes for Discord too)")
    reset()
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "orbital-dave")
    acc.link_discord(web, DISCORD)        # they linked Discord onto the web account
    member = _Member(DISCORD, "Dave")
    it = _Interaction(guild=_Guild([member]), user=member)
    tgt, err = run(resolve_err(it, member, None))
    check("the member resolves to the account, not the snowflake",
          tgt is not None and tgt.account_id == web and tgt.account_id != DISCORD)
    check("and is still mentionable", tgt is not None and tgt.member is member)
    check("the same account by username finds the member too",
          run(resolve_err(it, None, "orbital-dave"))[0].member is member)

    print("\nresolve — refusals")
    reset()
    member = _Member(DISCORD, "Jeb")
    it = _Interaction(guild=_Guild([member]), user=member)
    acc.ensure_discord_account(DISCORD, "jeb")
    acc.ensure_firebase_account(FBUID)
    acc.claim_username(acc.firebase_account_id(FBUID), "orbital-dave")

    tgt, err = run(resolve_err(it, member, "orbital-dave"))
    check("both fields is refused, not resolved", tgt is None and "not both" in err)

    tgt, err = run(resolve_err(it, None, None))
    check("no target is refused on a writing command", tgt is None and "Name someone" in err)

    tgt, err = run(resolve_err(it, None, None, default_self=True))
    check("no target means me on a reading command",
          tgt is not None and tgt.account_id == DISCORD)

    tgt, err = run(resolve_err(it, None, "nobody-at-all"))
    check("an unknown name is refused with a retype",
          tgt is None and "No Boundless account" in err)

    FAIL_READS.add("usernames")
    tgt, err = run(resolve_err(it, None, "orbital-dave"))
    check("an unreadable name refuses without acting",
          tgt is None and "Couldn't reach" in err and "No Boundless account" not in err)
    FAIL_READS.clear()

    FAIL_READS.add("account_discord")
    tgt, err = run(resolve_err(it, member, None))
    check("an unreadable index refuses rather than using the snowflake",
          tgt is None and "Couldn't reach" in err)
    FAIL_READS.clear()

    print("\nresolve — an account id typed into the username field")
    reset()
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    it = _Interaction(guild=_Guild(), user=_Member(DISCORD, "mod"))
    tgt, err = run(resolve_err(it, None, web))
    check("a real account id is accepted", tgt is not None and tgt.account_id == web)

    tgt, err = run(resolve_err(it, None, "a_does-not-exist"))
    check("a typo'd id is refused, so no empty wallet is minted",
          tgt is None and "No Boundless account" in err)

    FAKE_STORE.users["1902111111111111"] = {"balance": 5}
    tgt, err = run(resolve_err(it, None, "1902111111111111"))
    check("a pre-accounts player with only a wallet is still reachable",
          tgt is not None and tgt.account_id == "1902111111111111")

    reset()
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "123456")
    tgt, err = run(resolve_err(it, None, "123456"))
    check("a numeric username beats the id reading of the same string",
          tgt is not None and tgt.account_id == web)

    print("\nboard_name — leaderboards survive a web-only player")
    reset()
    member = _Member(DISCORD, "Jeb")
    guild = _Guild([member])
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "orbital-dave")
    acc.set_display_name(web, "Orbital Dave")
    check("a snowflake uses the member cache", tg.board_name(guild, DISCORD) == "Jeb")
    check("an account id does not raise", isinstance(tg.board_name(guild, web), str))
    run(tg.prefetch_names([DISCORD, web]))
    check("after prefetch it has a name",
          tg.board_name(guild, web) == "Orbital Dave (@orbital-dave)")
    check("a snowflake is never fetched", DISCORD not in tg._names)
    check("an unknown id degrades to the id, not an exception",
          tg.board_name(guild, "a_ghost") == "User a_ghost")
    check("a member who left the guild degrades too",
          tg.board_name(guild, OTHER) == f"User {OTHER}")

    print("\nautocomplete")
    reset()
    acc.ensure_firebase_account(FBUID)
    acc.claim_username(acc.firebase_account_id(FBUID), "orbital-dave")
    choices = run(tg.username_autocomplete(_Interaction(), "orb"))
    check("suggests the claimed name",
          [c.value for c in choices] == ["orbital-dave"])
    FAIL_READS.add("usernames")
    check("a cached prefix is answered without a read",
          [c.value for c in run(tg.username_autocomplete(_Interaction(), "orb"))]
          == ["orbital-dave"])
    tg._cache.clear()
    check("an uncached failure is empty, never an exception",
          run(tg.username_autocomplete(_Interaction(), "orb")) == [])
    FAIL_READS.clear()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())
