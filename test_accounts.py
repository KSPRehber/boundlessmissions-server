"""Offline exercise of data/accounts: resolution, fail-closed reads, linking, names.

Firestore is replaced by a dict-backed fake — including its transaction — so this
checks the decisions the module makes rather than the driver under it. The fake
can be told to fail a read, which is the only way to test the rule the module is
built around: an absent index entry means "identity", so a read that *failed* must
never be allowed to look absent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data import accounts as acc

# ── a fake Firestore ─────────────────────────────────────────────────────────

DATA: dict[str, dict] = {}      # "collection/doc" -> payload
FAIL_READS: set[str] = set()    # collection names whose reads raise


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

    def create(self, payload):
        """Real `create()` semantics: refuse if the document already exists.

        This is what makes a link code safe to key on its own 6-digit value — a
        collision must fail so the caller re-draws, where `set()` would silently
        overwrite one player's challenge with another's.
        """
        if self._path in DATA:
            from google.api_core import exceptions as _gexc
            raise _gexc.AlreadyExists(self._path)
        DATA[self._path] = dict(payload)

    def delete(self):
        DATA.pop(self._path, None)

    @property
    def reference(self):
        return _Ref(self._path)


class _Ref:
    """Just enough of a DocumentReference for `doc.reference.delete()`."""
    def __init__(self, path):
        self._path = path

    def delete(self):
        DATA.pop(self._path, None)


class _QueryDoc:
    def __init__(self, path):
        self.id = path.split("/", 1)[1]
        self.reference = _Ref(path)
        self._path = path

    def to_dict(self):
        return dict(DATA.get(self._path, {}))


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
        if filter is not None:            # FieldFilter form
            field, value = filter.field_path, filter.value
        return _Query(self._name, field, value)


class _Txn:
    """Writes apply immediately — enough for these tests, which check the
    decisions inside the transaction rather than Firestore's retry semantics."""
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
# The real decorator wraps a function expecting a transaction; ours just calls it.
acc.firestore = type("_FS", (), {"transactional": staticmethod(lambda fn: fn)})()


class _Store:
    """Models the real store's two-step: a record EXISTS as soon as anyone reads
    one, and separately may or may not hold anything. Conflating those is the bug
    `has_activity` exists to fix."""
    def __init__(self):
        self.users = {}          # uid -> record

    def has_user(self, uid):
        return str(uid) in self.users

    def get_user(self, _gid, uid):
        return self.users.setdefault(str(uid), {"xp": 0, "balance": 0,
                                                "rescues": 0, "unlocked_levels": []})

    # test helpers
    def touch(self, uid):
        """What signing in does: creates an empty record."""
        self.get_user(0, uid)

    def give_history(self, uid, xp=500):
        self.get_user(0, uid)["xp"] = xp


acc.store = _Store()


# ── assertions ───────────────────────────────────────────────────────────────
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def reset():
    DATA.clear()
    FAIL_READS.clear()
    acc.store.users.clear()


def main():
    DISCORD = "123456789012345678"
    FBUID = "AbCdEf0123456789AbCdEf0123"

    print("\nid shapes")
    check("a snowflake is a Discord account", acc.is_discord_account(DISCORD))
    check("a firebase account id is not", not acc.is_discord_account(acc.firebase_account_id(FBUID)))
    check("the two id spaces cannot collide",
          acc.firebase_account_id(FBUID) != DISCORD
          and acc.firebase_account_id(FBUID).startswith("a_"))

    print("\nresolution: the identity function")
    reset()
    check("an unseen Discord id resolves to itself",
          acc.account_for_discord(DISCORD) == DISCORD)
    check("an unseen Firebase uid resolves to its prefixed id",
          acc.account_for_firebase(FBUID) == "a_" + FBUID)

    print("\nresolution: the index holds only the exceptions")
    reset()
    DATA[f"account_discord/{DISCORD}"] = {"account_id": "a_other"}
    check("a rebound Discord id resolves to its account",
          acc.account_for_discord(DISCORD) == "a_other")

    print("\nresolution: a failed read must not look like an absent one")
    reset()
    FAIL_READS.add("account_discord")
    check("Discord resolution reports unknowable, not identity",
          acc.account_for_discord(DISCORD) is None)
    reset()
    FAIL_READS.add("account_firebase")
    check("Firebase resolution reports unknowable, not identity",
          acc.account_for_firebase(FBUID) is None)
    reset()
    DATA[f"account_discord/{DISCORD}"] = {"linked_at": "whenever"}   # no account_id
    check("a corrupt index row is unknowable, not identity",
          acc.account_for_discord(DISCORD) is None)

    print("\ncreation")
    reset()
    a = acc.ensure_discord_account(DISCORD, "Jeb")
    check("creates the account", a and a["account_id"] == DISCORD, a)
    check("keyed by the snowflake, so no migration is needed",
          f"accounts/{DISCORD}" in DATA)
    check("seeds display_name from Discord", a["display_name"] == "Jeb")
    check("and claims the Discord name as the username when it is free",
          a["username"] == "Jeb")
    again = acc.ensure_discord_account(DISCORD, "Jeb")
    check("is idempotent", again["created_at"] == a["created_at"])

    DATA[f"accounts/{DISCORD}"]["display_name"] = "Commander"
    acc.ensure_discord_account(DISCORD, "JebRenamed")
    check("a Discord rename refreshes the cached username",
          DATA[f"accounts/{DISCORD}"]["discord_username"] == "JebRenamed")
    check("but never overwrites the player's own display name",
          DATA[f"accounts/{DISCORD}"]["display_name"] == "Commander")

    print("\ncreation: the Discord username is taken when it is free")
    reset()
    a = acc.ensure_discord_account(DISCORD, "Jebediah")
    check("a free Discord name is claimed automatically",
          a["username"] == "Jebediah", a)
    check("so most players never see an onboarding prompt",
          DATA["usernames/jebediah"]["account_id"] == DISCORD)

    other = "222222222222222222"
    a2 = acc.ensure_discord_account(other, "Jebediah")
    check("a COLLIDING Discord name is not claimed", a2["username"] == "", a2)
    check("which is what sends that player to onboarding",
          DATA[f"accounts/{other}"]["username"] == "")
    check("and the first player keeps the name",
          DATA["usernames/jebediah"]["account_id"] == DISCORD)

    third = "333333333333333333"
    a3 = acc.ensure_discord_account(third, "x")   # too short for the rules
    check("a Discord name the rules reject is not claimed either", a3["username"] == "")

    print("\nDiscord link challenges")
    reset()
    acc.ensure_firebase_account(FBUID, email="jeb@example.com")
    web = acc.firebase_account_id(FBUID)
    made = acc.create_link_challenge(web)
    check("mints a 6-digit code", made and len(made[0]) == 6, made)
    code = made[0]

    peeked = acc.peek_link_challenge(code)
    check("peeking names the account on the other end",
          peeked and peeked["account_id"] == web, peeked)
    check("peeking does NOT spend it — the confirmation has to come first",
          acc.peek_link_challenge(code) is not None)

    check("consuming returns the account", acc.consume_link_challenge(code) == web)
    check("and it is spent", acc.peek_link_challenge(code) is None)
    check("an unknown code peeks to nothing", acc.peek_link_challenge("000000") is None)

    made2 = acc.create_link_challenge(web)
    made3 = acc.create_link_challenge(web)
    check("minting again burns the previous code, so no stale one lingers",
          acc.peek_link_challenge(made2[0]) is None
          and acc.peek_link_challenge(made3[0]) is not None)

    print("\ndeletion")
    reset()
    acc.ensure_discord_account(DISCORD, "Jebediah")
    acc.link_firebase(DISCORD, FBUID, email="j@e.com")
    acc.create_link_challenge(DISCORD)
    removed = acc.delete_account(DISCORD)
    check("removes the account document", removed["account"] and f"accounts/{DISCORD}" not in DATA)
    check("frees the username", removed["username"] == "Jebediah"
          and "usernames/jebediah" not in DATA)
    check("drops the firebase index row", removed["firebase_index"]
          and f"account_firebase/{FBUID}" not in DATA)
    check("and any outstanding link codes", removed["link_codes"] == 1)
    check("so the name can be claimed by someone else afterwards",
          acc.account_for_username("jebediah") is None)

    print("\ndeletion does not free a username that has moved on")
    reset()
    acc.ensure_discord_account(DISCORD, "Jebediah")
    # Someone else now holds the reservation (a re-point, however it happened).
    DATA["usernames/jebediah"]["account_id"] = "a_someone_else"
    removed = acc.delete_account(DISCORD)
    check("the reservation is left alone", removed["username"] == ""
          and DATA["usernames/jebediah"]["account_id"] == "a_someone_else")

    print("\nactivity is not existence")
    reset()
    acc.store.touch(DISCORD)
    check("merely signing in does not count as history",
          not acc.has_activity(DISCORD))
    acc.store.give_history(DISCORD)
    check("earning something does", acc.has_activity(DISCORD))
    check("an untouched account has nothing", not acc.has_activity("999"))

    print("\njoining: the account with the history survives")
    reset()
    acc.ensure_discord_account(DISCORD, "Jeb")
    acc.ensure_firebase_account(FBUID, email="jeb@example.com", display_name="Web Jeb")
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "boundless-guy")
    acc.store.give_history(DISCORD)      # played on Discord
    acc.store.touch(web)                 # just signed up on the site

    code, msg, kept = acc.join_accounts(DISCORD, web)
    check("joins", code == acc.JOIN_OK, (code, msg))
    check("keeping the Discord account, which holds the history", kept == DISCORD, kept)
    check("the website sign-in now reaches it",
          acc.account_for_firebase(FBUID) == DISCORD)
    check("the drained account is gone", f"accounts/{web}" not in DATA)
    check("its username is released, not left pointing at nothing",
          acc.account_for_username("boundless-guy") is None)
    check("and the survivor keeps its own name",
          DATA[f"accounts/{DISCORD}"]["username"] == "Jeb")
    check("inheriting the email it had none of",
          DATA[f"accounts/{DISCORD}"]["email"] == "jeb@example.com")

    print("\njoining: the other way round")
    reset()
    acc.ensure_discord_account(DISCORD, "")      # fresh Discord, no name
    acc.ensure_firebase_account(FBUID, email="j@e.com")
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "boundless-guy")
    acc.store.give_history(web)                  # the website side is the real one
    acc.store.touch(DISCORD)

    code, msg, kept = acc.join_accounts(DISCORD, web)
    check("keeps the website account", code == acc.JOIN_OK and kept == web, (code, kept))
    check("and Discord now reaches it", acc.account_for_discord(DISCORD) == web)
    check("the name survives with it",
          acc.account_for_username("boundless-guy") == web)

    print("\njoining: an unnamed survivor inherits the name")
    reset()
    acc.ensure_discord_account(DISCORD, "")      # Discord name was unavailable
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.claim_username(web, "boundless-guy")
    acc.store.give_history(DISCORD)
    code, _msg, kept = acc.join_accounts(DISCORD, web)
    check("Discord survives", kept == DISCORD)
    check("but takes the name rather than losing it",
          DATA[f"accounts/{DISCORD}"]["username"] == "boundless-guy"
          and acc.account_for_username("boundless-guy") == DISCORD)

    print("\njoining: the dropped account's sessions must not survive it")
    reset()
    acc.ensure_discord_account(DISCORD, "Jeb")
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.store.give_history(DISCORD)
    revoked = []
    import api_auth
    api_auth.logout_all_devices = lambda uid: revoked.append(str(uid))
    acc.join_accounts(DISCORD, web)
    check("the dropped account is logged out everywhere", revoked == [web], revoked)
    check("the survivor is left alone — joining must not sign you out of the "
          "account you kept", DISCORD not in revoked)

    print("\njoining: two real histories is a merge, and is refused")
    reset()
    acc.ensure_discord_account(DISCORD, "Jeb")
    acc.ensure_firebase_account(FBUID)
    web = acc.firebase_account_id(FBUID)
    acc.store.give_history(DISCORD)
    acc.store.give_history(web)
    code, msg, kept = acc.join_accounts(DISCORD, web)
    check("refused", code == acc.JOIN_BOTH_ACTIVE, (code, msg))
    check("nothing was moved", acc.account_for_firebase(FBUID) == web)
    check("and both accounts still exist",
          f"accounts/{DISCORD}" in DATA and f"accounts/{web}" in DATA)

    print("\nlinking: the second-wallet guard")
    reset()
    acc.ensure_firebase_account(FBUID, email="a@b.c")
    web = acc.firebase_account_id(FBUID)

    acc.store.give_history(DISCORD)           # that Discord player has real history
    code, msg = acc.link_discord(web, DISCORD)
    check("linking a Discord account that already has data is refused",
          code == acc.LINK_HAS_DATA, (code, msg))
    check("nothing was written", f"account_discord/{DISCORD}" not in DATA)

    acc.store.users.pop(DISCORD, None)        # a Discord account with no history
    code, _ = acc.link_discord(web, DISCORD)
    check("linking a fresh Discord account succeeds", code == acc.LINK_OK)
    check("the index now redirects it",
          acc.account_for_discord(DISCORD) == web)
    check("and the account records it",
          DATA[f"accounts/{web}"]["discord_id"] == DISCORD)
    code, _ = acc.link_discord(web, DISCORD)
    check("relinking the same one is a no-op", code == acc.LINK_ALREADY)

    code, _ = acc.link_discord(web, "999888777666555444")
    check("a second, different Discord account is refused",
          code == acc.LINK_CONFLICT)

    print("\nlinking: unknowable is refused, never guessed")
    reset()
    acc.ensure_firebase_account(FBUID)
    FAIL_READS.add("account_discord")
    code, _ = acc.link_discord(acc.firebase_account_id(FBUID), DISCORD)
    check("a failed check refuses the link", code == acc.LINK_ERROR)

    print("\nusernames: validation")
    check("too short", acc.validate_username("ab") is not None)
    check("too long", acc.validate_username("x" * 21) is not None)
    check("spaces refused", acc.validate_username("je b") is not None)
    check("leading punctuation refused", acc.validate_username("-jeb") is not None)
    check("trailing punctuation refused", acc.validate_username("jeb-") is not None)
    check("reserved refused", acc.validate_username("Admin") is not None)
    check("reserved is case-insensitive", acc.validate_username("aDmIn") is not None)
    check("a normal name passes", acc.validate_username("Jeb_Kerman-1") is None)

    print("\nusernames: claiming")
    reset()
    # No Discord name to auto-claim, so this account arrives unnamed — the state
    # onboarding exists for, and the one where claim_username is the only writer.
    acc.ensure_discord_account(DISCORD, "")
    ok, msg = acc.claim_username(DISCORD, "Jebediah")
    check("claims it", ok and msg == "Jebediah", msg)
    check("reserves it under the normalized form", "usernames/jebediah" in DATA)

    other2 = "444444444444444444"
    acc.ensure_discord_account(other2, "")
    ok, msg = acc.claim_username(other2, "JEBEDIAH")
    check("a differently-cased duplicate is refused", not ok, msg)
    check("which is what stops impersonation",
          DATA["usernames/jebediah"]["account_id"] == DISCORD)

    ok, msg = acc.claim_username(DISCORD, "SomethingElse")
    check("a claimed username cannot be changed", not ok, msg)
    check("and the second name was not reserved", "usernames/somethingelse" not in DATA)

    ok, _ = acc.claim_username(DISCORD, "Jebediah")
    check("re-claiming your own name is idempotent", ok)

    ok, msg = acc.claim_username("nosuchaccount", "Ghost")
    check("claiming for a missing account is refused", not ok, msg)

    print("\nusernames: lookup")
    check("resolves an owner", acc.account_for_username("jEbEdIaH") == DISCORD)
    check("unknown name is None", acc.account_for_username("nobody") is None)

    print("\ndisplay name")
    reset()
    acc.ensure_discord_account(DISCORD, "")
    ok, _ = acc.set_display_name(DISCORD, "  Commander Jeb  ")
    check("trims and stores", ok and DATA[f"accounts/{DISCORD}"]["display_name"] == "Commander Jeb")
    ok, msg = acc.set_display_name(DISCORD, "   ")
    check("empty refused", not ok, msg)
    ok, msg = acc.set_display_name(DISCORD, "x" * 33)
    check("over-long refused", not ok, msg)
    check("neither wrote anything",
          DATA[f"accounts/{DISCORD}"]["display_name"] == "Commander Jeb")

    print("\ndiscord_for_account")
    reset()
    check("a Discord account answers without a read",
          acc.discord_for_account(DISCORD) == DISCORD)
    acc.ensure_firebase_account(FBUID)
    check("a web-only account has no Discord",
          acc.discord_for_account(acc.firebase_account_id(FBUID)) == "")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())
