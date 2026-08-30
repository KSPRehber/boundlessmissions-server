"""
data/accounts.py – who a player *is*, separately from where they signed up.

Until now the Discord snowflake was the primary key of everything: `users/{id}`,
`ksp_sessions/{id}`, `guilds/{gid}/corps/{id}`, every `seller_id` and `issuer_id`.
That is fine right up until someone wants an account without joining a Discord
server, at which point there is no key to give them.

This module introduces one indirection and nothing else. An **account id** is an
opaque string; the session token carries it as `uid`, and every existing endpoint
keeps working unchanged because the security spine — token versioning, device
binding, suspensions — has always treated `uid` as opaque and never parsed it.

The trick that makes this a change of *meaning* rather than a data migration:

    a Discord-origin account's id IS the snowflake.       accounts/{snowflake}
    a Firebase-origin account's id is "a_" + firebase_uid. accounts/a_xxxxx

So for every user who exists today `account_for_discord(id) == str(id)` — the
identity function — and not one document has to move. The `a_` prefix is what
guarantees the two namespaces can never collide, and makes an id self-describing
at a glance.

The indexes hold only the EXCEPTIONS to that rule: a Discord id that resolves to
some other account (because a Discord-less account linked it later), or a
Firebase uid that resolves to a Discord-origin account (because an existing
player added a Google login). Both are empty for the common case.

That design has one sharp edge, and it is the reason for the fail-closed rule
below: because an *absent* index entry means "identity", a failed read that
returned "absent" would silently resolve a rebound account back to its snowflake
and hand the player a brand-new empty wallet. So every resolver here distinguishes
"there is no entry" from "I could not find out", and returns None for the latter.
Callers must refuse rather than guess. This is the same distinction — and the same
reasoning — as `api_auth._get_allowed_devices` returning None instead of an empty
set on a failed read.
"""

import logging
import re
import time
from datetime import datetime, timezone

from firebase_admin import firestore

from data.store import _db, store

log = logging.getLogger(__name__)

# Firebase-origin account ids carry this prefix. Snowflakes are all digits, so the
# prefix is what keeps the two id spaces provably disjoint.
FIREBASE_PREFIX = "a_"


def _accounts():
    return _db.collection("accounts")


def _discord_index():
    """discord_id -> account_id, for Discord ids that are NOT their own account."""
    return _db.collection("account_discord")


def _firebase_index():
    """firebase_uid -> account_id, for Firebase uids that are NOT their own account."""
    return _db.collection("account_firebase")


def _usernames():
    """lowercased username -> account_id. Firestore has no unique constraint, so
    the reservation IS the constraint: the document id is the name."""
    return _db.collection("usernames")


# ── Id shapes ────────────────────────────────────────────────────────────────

def is_discord_account(account_id) -> bool:
    """Whether this account id is a Discord snowflake (and so has a Discord user
    behind it that can be mentioned, DM'd and given channel permissions)."""
    return str(account_id).isdigit()


def firebase_account_id(firebase_uid: str) -> str:
    """The account id a Firebase uid owns by default."""
    return FIREBASE_PREFIX + str(firebase_uid)


# ── Resolution ───────────────────────────────────────────────────────────────
#
# Every resolver returns None to mean "unknowable right now", never to mean "no
# account". Read the module docstring before changing that.

def account_for_discord(discord_id) -> str | None:
    """The account id a Discord user signs in as, or None if it cannot be read.

    Absent index entry → the snowflake itself, which is the answer for everybody
    who has ever used this bot. An entry exists only when this Discord account was
    linked onto an account that already existed without it.
    """
    did = str(discord_id)
    try:
        snap = _discord_index().document(did).get()
    except Exception as exc:
        log.warning("Could not resolve Discord id %s to an account: %s", did, exc)
        return None
    if snap.exists:
        mapped = (snap.to_dict() or {}).get("account_id")
        if mapped:
            return str(mapped)
        # An index row with no target is corrupt, not empty. Falling through to
        # the identity answer would hand out the wrong account, so refuse.
        log.error("Discord index row %s has no account_id", did)
        return None
    return did


def account_for_firebase(firebase_uid: str) -> str | None:
    """The account id a Firebase (Google / email) user signs in as, or None if it
    cannot be read. Mirror image of `account_for_discord`."""
    uid = str(firebase_uid)
    try:
        snap = _firebase_index().document(uid).get()
    except Exception as exc:
        log.warning("Could not resolve Firebase uid %s to an account: %s", uid, exc)
        return None
    if snap.exists:
        mapped = (snap.to_dict() or {}).get("account_id")
        if mapped:
            return str(mapped)
        log.error("Firebase index row %s has no account_id", uid)
        return None
    return firebase_account_id(uid)


def get_account(account_id) -> dict | None:
    """The account document, or None if missing or unreadable.

    Deliberately does NOT create one. An account is created by an explicit
    `ensure_*` call at a moment when we know who is asking; inventing one from a
    read would let a typo'd id mint an account.
    """
    try:
        snap = _accounts().document(str(account_id)).get()
    except Exception as exc:
        log.warning("Could not read account %s: %s", account_id, exc)
        return None
    return snap.to_dict() if snap.exists else None


def discord_for_account(account_id) -> str:
    """The Discord id attached to an account, or "" if it has none.

    Cheap for the common case: a Discord-origin account's id already IS the
    snowflake, so no read happens at all.
    """
    aid = str(account_id)
    if is_discord_account(aid):
        return aid
    acct = get_account(aid)
    return str((acct or {}).get("discord_id") or "")


# ── Creation ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_discord_account(discord_id, username: str = "") -> dict | None:
    """The account document for a Discord user, created if this is the first time.

    Called where a link completes, which is the one moment the server knows a
    Discord user has accepted the terms in-mod — the same evidence
    `corps.ensure_corp_for_linked_user` hangs off. Returns None if Firestore
    cannot be reached; the caller decides whether that is fatal (it usually is
    not: nothing reads the account document yet).
    """
    did = str(discord_id)
    aid = account_for_discord(did)
    if aid is None:
        return None

    existing = get_account(aid)
    if existing is not None:
        # Keep the cached Discord username fresh, but never touch display_name:
        # that is the player's own, and a Discord rename must not overwrite it.
        if username and existing.get("discord_username") != username:
            try:
                _accounts().document(aid).set(
                    {"discord_username": username}, merge=True)
                existing["discord_username"] = username
            except Exception as exc:
                log.warning("Could not refresh username on account %s: %s", aid, exc)
        return existing

    doc = {
        "account_id": aid,
        "discord_id": did,
        "discord_username": username,
        "username": "",          # claimed just below, or in onboarding
        "display_name": username,
        "avatar_url": "",
        "created_at": _now(),
        "created_via": "discord",
    }
    try:
        _accounts().document(aid).set(doc)
        log.info("Created account %s for Discord user %s", aid, did)
    except Exception as exc:
        log.warning("Could not create account for Discord user %s: %s", did, exc)
        return None

    # Take their Discord name as the Boundless username when it is free and legal.
    # It is the name they already answer to, so asking them to retype it would be
    # ceremony — but it CANNOT be assumed: two Discord accounts in different
    # servers can share a display name, plenty of Discord names are shapes this
    # does not allow (dots, two characters, a reserved word), and the username is
    # permanent and public. So a claim that does not go through leaves the field
    # empty, which is what `needs_onboarding` reads, and they pick one themselves.
    if username:
        ok, _msg = claim_username(aid, username)
        if ok:
            doc["username"] = str(username).strip()
        else:
            log.info("Account %s must choose a username (%r unavailable)", aid, username)
    return doc


def ensure_firebase_account(firebase_uid: str, *, email: str = "",
                            display_name: str = "") -> dict | None:
    """The account document for a Google / email sign-in, created on first use."""
    uid = str(firebase_uid)
    aid = account_for_firebase(uid)
    if aid is None:
        return None

    existing = get_account(aid)
    if existing is not None:
        return existing

    doc = {
        "account_id": aid,
        "firebase_uid": uid,
        "email": email,
        "username": "",
        "display_name": display_name,
        "avatar_url": "",
        "created_at": _now(),
        "created_via": "firebase",
    }
    try:
        _accounts().document(aid).set(doc)
        log.info("Created account %s for Firebase uid %s", aid, uid)
    except Exception as exc:
        log.warning("Could not create account for Firebase uid %s: %s", uid, exc)
        return None
    return doc


# ── Does this account actually hold anything? ────────────────────────────────

def has_activity(account_id) -> bool:
    """Whether an account holds anything a merge would have to preserve.

    Emphatically NOT "does a record exist". `store.get_user` CREATES a default
    record as a side effect of reading one, so merely signing in — or linking a
    game, or loading a profile page — is enough to make a record exist. An
    existence check therefore answers "yes" for every account anyone has ever
    touched, which made the merge guard below refuse every genuine link.

    So the question is whether the record has anything IN it: progress, money
    that isn't the starting float, a rescue, an unlocked achievement level. A
    freshly created account has none of those and can be safely absorbed.
    """
    import settings
    if not store.has_user(account_id):
        return False
    u = store.get_user(0, account_id)
    return bool(
        int(u.get("xp", 0) or 0) > 0
        or int(u.get("balance", 0) or 0) != int(settings.STARTING_BALANCE)
        or int(u.get("rescues", 0) or 0) > 0
        or (u.get("unlocked_levels") or [])
    )


# ── Linking an identity onto an existing account ─────────────────────────────
#
# This is the path that can mint a second wallet if it is wrong, so it refuses
# anything it is not certain about.

LINK_OK = "ok"
LINK_ALREADY = "already"          # already linked to this same account — no-op
LINK_TAKEN = "taken"              # that identity belongs to a different account
LINK_HAS_DATA = "has_data"        # …and that other account has a wallet of its own
LINK_CONFLICT = "conflict"        # this account already has a different identity
LINK_ERROR = "error"              # could not find out; try again


def link_discord(account_id, discord_id) -> tuple[str, str]:
    """Attach a Discord identity to an existing account. Returns (code, message).

    The case worth being careful about is a player who already played on Discord
    and has now signed up on the website: two accounts, both with a balance, both
    theirs. There is no safe automatic answer to that — merging means rewriting
    every `seller_id`, `issuer_id` and corp document that names the old id, and
    picking the wrong direction silently destroys one of the two wallets. So it is
    refused here and left to a human, which is the right cost for something this
    rare and this destructive.
    """
    aid = str(account_id)
    did = str(discord_id)

    current = account_for_discord(did)
    if current is None:
        return LINK_ERROR, "Couldn't check that Discord account just now. Try again."

    if current == aid:
        return LINK_ALREADY, "That Discord account is already linked."

    # The Discord id resolves somewhere else. If that somewhere has a wallet, it is
    # a real account and this is a merge, not a link.
    if has_activity(current):
        return LINK_HAS_DATA, (
            "That Discord account already has a Boundless Missions account with "
            "its own balance and history. Sign in with it instead, or open a "
            "ticket if you need the two combined.")

    acct = get_account(aid)
    if acct is None:
        return LINK_ERROR, "Couldn't read your account just now. Try again."
    existing_did = str(acct.get("discord_id") or "")
    if existing_did and existing_did != did:
        return LINK_CONFLICT, (
            "Your account is already linked to a different Discord account. "
            "Unlink that one first.")

    try:
        _discord_index().document(did).set({
            "account_id": aid,
            "linked_at": _now(),
        })
        _accounts().document(aid).set({"discord_id": did}, merge=True)
    except Exception as exc:
        log.warning("Could not link Discord %s to account %s: %s", did, aid, exc)
        return LINK_ERROR, "Couldn't save that link just now. Try again."

    log.info("Linked Discord %s to account %s", did, aid)
    return LINK_OK, "Discord account linked."


def link_firebase(account_id, firebase_uid: str, *, email: str = "") -> tuple[str, str]:
    """Attach a Google / email identity to an existing account. Mirror of
    `link_discord`, and refuses the same merge case for the same reason."""
    aid = str(account_id)
    uid = str(firebase_uid)

    current = account_for_firebase(uid)
    if current is None:
        return LINK_ERROR, "Couldn't check that sign-in just now. Try again."
    if current == aid:
        return LINK_ALREADY, "That sign-in is already linked."
    if has_activity(current):
        return LINK_HAS_DATA, (
            "That Google account already has a Boundless Missions account with its "
            "own balance and history. Sign in with it instead, or open a ticket if "
            "you need the two combined.")

    acct = get_account(aid)
    if acct is None:
        return LINK_ERROR, "Couldn't read your account just now. Try again."
    existing_uid = str(acct.get("firebase_uid") or "")
    if existing_uid and existing_uid != uid:
        return LINK_CONFLICT, (
            "Your account already has a different sign-in attached. "
            "Remove that one first.")

    try:
        _firebase_index().document(uid).set({
            "account_id": aid,
            "linked_at": _now(),
        })
        patch = {"firebase_uid": uid}
        if email:
            patch["email"] = email
        _accounts().document(aid).set(patch, merge=True)
    except Exception as exc:
        log.warning("Could not link Firebase %s to account %s: %s", uid, aid, exc)
        return LINK_ERROR, "Couldn't save that link just now. Try again."

    log.info("Linked Firebase uid %s to account %s", uid, aid)
    return LINK_OK, "Sign-in linked."


# ── Usernames ────────────────────────────────────────────────────────────────
#
# The username is permanent and public. That is a product decision, but it has a
# storage consequence: there is no rename to fall back on, so everything that
# would normally be fixed later — a name someone else wanted, a slur, an
# impersonation — has to be caught at the one moment it is claimed.

USERNAME_MIN = 3
USERNAME_MAX = 20
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")

# Names that must never belong to a player, because seeing one next to a message
# is itself a claim of authority. Matched against the normalized (lowercased) form.
RESERVED_USERNAMES = {
    "admin", "administrator", "mod", "moderator", "staff", "owner", "root",
    "system", "support", "help", "official", "boundless", "boundlessmissions",
    "genekerman", "gene", "kerbal", "ksp", "bot", "server", "api", "null",
    "undefined", "anonymous", "deleted", "me", "you", "everyone", "here",
}


def normalize_username(name: str) -> str:
    """The form a username is stored and compared under. Case is presentation
    only: `Jeb` and `jeb` are the same claim, and letting both exist is how
    impersonation gets in."""
    return str(name or "").strip().lower()


def validate_username(name: str) -> str | None:
    """None if the name may be claimed, otherwise the reason it may not.

    Returns a sentence for the player, not an error code — this is shown directly
    in onboarding, where a rejection with no reason just gets retried verbatim.
    """
    raw = str(name or "").strip()
    if len(raw) < USERNAME_MIN:
        return f"Usernames need at least {USERNAME_MIN} characters."
    if len(raw) > USERNAME_MAX:
        return f"Usernames can be at most {USERNAME_MAX} characters."
    if not _USERNAME_RE.match(raw):
        return ("Usernames can use letters, numbers, hyphens and underscores, "
                "and must start and end with a letter or number.")
    if normalize_username(raw) in RESERVED_USERNAMES:
        return "That username is reserved."
    return None


def claim_username(account_id, name: str) -> tuple[bool, str]:
    """Claim a username permanently. Returns (ok, message).

    Firestore has no unique constraint, so the reservation document *is* the
    constraint: its id is the normalized name, and a transaction is what stops two
    people claiming the same one in the same instant. The account's own field is
    written inside that transaction too, so a reservation can never exist without
    the account that owns it.
    """
    aid = str(account_id)
    problem = validate_username(name)
    if problem:
        return False, problem

    raw = str(name).strip()
    key = normalize_username(raw)
    uname_ref = _usernames().document(key)
    acct_ref = _accounts().document(aid)

    try:
        transaction = _db.transaction()

        @firestore.transactional
        def _claim(txn) -> str:
            uname_snap = uname_ref.get(transaction=txn)
            acct_snap = acct_ref.get(transaction=txn)
            if not acct_snap.exists:
                return "no_account"
            existing = str((acct_snap.to_dict() or {}).get("username") or "")
            if existing:
                # Permanent means permanent — but re-sending the SAME name is a
                # retry, not a rename. Onboarding double-submits (an impatient
                # second click, a retried request) must not report the name the
                # player just successfully claimed as a failure.
                return "mine" if normalize_username(existing) == key else "already_set"
            if uname_snap.exists:
                owner = (uname_snap.to_dict() or {}).get("account_id")
                return "mine" if str(owner) == aid else "taken"
            txn.set(uname_ref, {"account_id": aid, "claimed_at": _now()})
            txn.set(acct_ref, {"username": raw}, merge=True)
            return "ok"

        outcome = _claim(transaction)
    except Exception as exc:
        log.warning("Username claim %r for %s failed: %s", raw, aid, exc)
        return False, "Couldn't save that username just now. Try again."

    if outcome == "ok":
        log.info("Account %s claimed username %r", aid, raw)
        return True, raw
    if outcome == "mine":
        return True, raw
    if outcome == "already_set":
        return False, "Your username is already set and can't be changed."
    if outcome == "no_account":
        return False, "Couldn't find your account."
    return False, "That username is taken."


def account_for_username(name: str) -> str | None:
    """Who owns a username, or None if nobody does / it cannot be read.

    Note this cannot distinguish "free" from "unreadable", and must not be used to
    decide a claim — `claim_username`'s transaction is the only safe test. It is
    for looking a player up, where a wrong answer costs a search result.
    """
    key = normalize_username(name)
    if not key:
        return None
    try:
        snap = _usernames().document(key).get()
    except Exception as exc:
        log.warning("Could not look up username %r: %s", key, exc)
        return None
    return str((snap.to_dict() or {}).get("account_id")) if snap.exists else None


def owner_of_username(name: str) -> str | None:
    """`account_for_username`, with "nobody" and "couldn't read" pulled apart.

    `account_for_username` collapses both into None, which is exactly right for a
    search box — a result that isn't there and a result that couldn't be fetched
    both render as no result — and exactly wrong for a moderator command, where
    the two have opposite answers. "No account by that name" means *retype it*;
    "couldn't read it" means *don't retype it, and don't act*. A Firestore blip
    reported as the first sends a moderator hunting for a typo in a name that was
    correct, and — worse — makes "this user doesn't exist" the story they take
    back to the ticket.

    Returns the account id, "" when the name is genuinely unclaimed, or None when
    the lookup failed. Same fail-closed contract as every resolver above: None is
    never an answer, only the absence of one.
    """
    key = normalize_username(name)
    if not key:
        return ""
    try:
        snap = _usernames().document(key).get()
    except Exception as exc:
        log.warning("Could not look up username %r: %s", key, exc)
        return None
    if not snap.exists:
        return ""
    owner = str((snap.to_dict() or {}).get("account_id") or "")
    if not owner:
        # A reservation with no owner is corrupt, not free. Answering "" would
        # invite a moderator to conclude the account was deleted.
        log.error("Username reservation %r has no account_id", key)
        return None
    return owner


def search_usernames(prefix: str, limit: int = 25) -> list[str]:
    """Claimed usernames starting with `prefix`, for a picker.

    Reads the reservation collection rather than `accounts`, for two reasons: its
    document *id* is the normalized name, so a prefix scan is a document-id range
    query needing no index and no extra field, and the id is the whole answer —
    the row's contents are never fetched into the result, so the query costs at
    most `limit` reads and returns the exact strings `owner_of_username` will
    later be given back.

    Names come back lowercased because that is how they are stored; case is
    presentation only (see `normalize_username`), so a moderator picking one from
    this list and a player typing it with capitals resolve to the same account.
    """
    key = normalize_username(prefix)
    limit = max(1, min(int(limit or 25), 25))
    try:
        # `\uf8ff` is above every character a username may contain (the set is
        # ASCII alphanumerics, hyphen and underscore), so it closes the range on
        # any name that begins with `key`.
        q = (_usernames()
             .order_by("__name__")
             .start_at({"__name__": key})
             .end_at({"__name__": key + "\uf8ff"})
             .limit(limit))
        return [doc.id for doc in q.stream()]
    except Exception as exc:
        log.warning("Username search %r failed: %s", key, exc)
        return []


# ── Profile fields the player may change ─────────────────────────────────────

DISPLAY_NAME_MAX = 32


def set_display_name(account_id, name: str) -> tuple[bool, str]:
    """Set the changeable display name. Unlike the username this carries no
    uniqueness — two people may both call themselves Jeb, which is why the
    permanent username exists alongside it."""
    raw = str(name or "").strip()
    if not raw:
        return False, "Display names can't be empty."
    if len(raw) > DISPLAY_NAME_MAX:
        return False, f"Display names can be at most {DISPLAY_NAME_MAX} characters."
    try:
        _accounts().document(str(account_id)).set({"display_name": raw}, merge=True)
    except Exception as exc:
        log.warning("Could not set display name for %s: %s", account_id, exc)
        return False, "Couldn't save that just now. Try again."
    return True, raw


def set_avatar_url(account_id, url: str) -> bool:
    """Point the account at an already-stored avatar. Uploading it is the caller's
    job — this only records where it landed."""
    try:
        _accounts().document(str(account_id)).set(
            {"avatar_url": str(url or "")}, merge=True)
        return True
    except Exception as exc:
        log.warning("Could not set avatar for %s: %s", account_id, exc)
        return False


# ── Linking a Discord account to a website account ───────────────────────────
#
# The code is minted in the website panel (where being signed in proves control of
# the Google/email identity) and typed into Discord (where running the command
# proves control of the Discord account). Doing both is the proof; there is no
# third step, because unlike a *session* link this creates no credential — it only
# joins two identities that the same person has just demonstrated they hold.
#
# The attack it still has to answer is someone talking a victim into running the
# command with the ATTACKER's code, which would bind the victim's Discord to the
# attacker's account. That is why `peek_link_challenge` exists separately from
# `consume_link_challenge`: Discord shows whose account is on the other end and
# waits for an explicit confirmation, so the trick has to survive being named.

ACCOUNT_LINK_CODE_LIFETIME = 600  # 10 minutes, like the panel's KSP code


def _link_codes():
    return _db.collection("account_link_codes")


def _digits(n: int = 6) -> str:
    import secrets
    return "".join(secrets.choice("0123456789") for _ in range(n))


def create_link_challenge(account_id) -> tuple[str, float] | None:
    """Mint a code that links this account to whichever Discord runs it.

    Any outstanding code for the account is burned first: two live codes for one
    account means a stale one someone could still be talked into using.
    """
    aid = str(account_id)
    try:
        for doc in _link_codes().where("account_id", "==", aid).stream():
            doc.reference.delete()
        code = _digits(6)
        expires_at = time.time() + ACCOUNT_LINK_CODE_LIFETIME
        _link_codes().document(code).set({
            "account_id": aid,
            "created_at": _now(),
            "expires_at": expires_at,
        })
        log.info("Created Discord-link challenge for account %s", aid)
        return code, expires_at
    except Exception as exc:
        log.warning("Could not create link challenge for %s: %s", aid, exc)
        return None


def peek_link_challenge(code: str) -> dict | None:
    """Who a code belongs to, WITHOUT spending it.

    Split from consuming on purpose: the person about to link has to be shown
    whose account they would be joining before they agree to it. Returns the
    account document (plus `account_id`), or None if the code is unknown/expired.
    """
    try:
        snap = _link_codes().document(str(code).strip()).get()
    except Exception as exc:
        log.warning("Could not read link challenge: %s", exc)
        return None
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    if time.time() > data.get("expires_at", 0):
        try:
            snap.reference.delete()
        except Exception:
            pass
        return None
    acct = get_account(data.get("account_id"))
    if acct is None:
        return None
    acct = dict(acct)
    acct["account_id"] = str(data.get("account_id"))
    return acct


def consume_link_challenge(code: str) -> str | None:
    """Spend a code and return the account id it named, or None."""
    key = str(code).strip()
    peeked = peek_link_challenge(key)
    if peeked is None:
        return None
    try:
        _link_codes().document(key).delete()
    except Exception as exc:
        log.warning("Could not consume link challenge: %s", exc)
    return peeked["account_id"]


# ── Deletion (moderation / debugging) ────────────────────────────────────────

def delete_account(account_id) -> dict:
    """Erase the identity records for an account. Returns what was removed.

    Deliberately NOT the whole player: the wallet (`users/{id}`), their listings
    and their contracts are somebody else's counterparty and are handled by their
    own admin paths. This is the identity half — the account document, the
    username reservation and both index rows — and it is what makes an id
    reusable for testing instead of permanently poisoned.

    The username reservation is released only when it actually belongs to this
    account. A reservation is the uniqueness constraint, so freeing one that has
    since been re-pointed elsewhere would let two accounts hold the same name.
    """
    aid = str(account_id)
    removed = {"account": False, "username": "", "discord_index": False,
               "firebase_index": False, "link_codes": 0}

    acct = get_account(aid) or {}

    uname = str(acct.get("username") or "")
    if uname:
        key = normalize_username(uname)
        try:
            snap = _usernames().document(key).get()
            if snap.exists and str((snap.to_dict() or {}).get("account_id")) == aid:
                _usernames().document(key).delete()
                removed["username"] = uname
        except Exception as exc:
            log.warning("delete_account: username %r for %s: %s", uname, aid, exc)

    did = str(acct.get("discord_id") or "")
    if did:
        try:
            snap = _discord_index().document(did).get()
            if snap.exists and str((snap.to_dict() or {}).get("account_id")) == aid:
                _discord_index().document(did).delete()
                removed["discord_index"] = True
        except Exception as exc:
            log.warning("delete_account: discord index for %s: %s", aid, exc)

    fuid = str(acct.get("firebase_uid") or "")
    if fuid:
        try:
            snap = _firebase_index().document(fuid).get()
            if snap.exists and str((snap.to_dict() or {}).get("account_id")) == aid:
                _firebase_index().document(fuid).delete()
                removed["firebase_index"] = True
        except Exception as exc:
            log.warning("delete_account: firebase index for %s: %s", aid, exc)

    try:
        for doc in _link_codes().where("account_id", "==", aid).stream():
            doc.reference.delete()
            removed["link_codes"] += 1
    except Exception as exc:
        log.warning("delete_account: link codes for %s: %s", aid, exc)

    try:
        if acct:
            _accounts().document(aid).delete()
            removed["account"] = True
    except Exception as exc:
        log.warning("delete_account: account doc %s: %s", aid, exc)

    log.warning("Deleted account identity records for %s: %s", aid, removed)
    return removed


# ── Joining a Discord account and a website account ──────────────────────────

JOIN_OK = "ok"
JOIN_SAME = "same"             # already one account
JOIN_BOTH_ACTIVE = "both"      # two real histories — a merge, and not ours to do
JOIN_ERROR = "error"


def join_accounts(discord_account_id, web_account_id) -> tuple[str, str, str]:
    """Make a Discord account and a website account into one. Returns
    (code, message, surviving_account_id).

    **Which one survives is the whole problem.** Linking is not symmetric: the
    account that loses is the one whose id stops resolving, and every `seller_id`,
    `issuer_id`, corp document and wallet still names it. Point a Discord id at a
    fresh website account and you have quietly orphaned that player's entire game
    history — which is exactly what the first version of this did.

    So the rule is: **the account holding the history survives, and the other one's
    way of signing in is moved onto it.** Someone who has played for months and now
    wants a Google button keeps everything and gains a button. Only when BOTH sides
    have a real history is this a merge — rewriting references, picking a winner,
    destroying one of two balances — and that is refused for a human to sort out.
    """
    d_id = str(discord_account_id)
    w_id = str(web_account_id)
    if d_id == w_id:
        return JOIN_SAME, "Those are already the same account.", d_id

    d_acct = get_account(d_id)
    w_acct = get_account(w_id)
    if d_acct is None or w_acct is None:
        return JOIN_ERROR, "Couldn't read one of those accounts. Try again.", ""

    d_active = has_activity(d_id)
    w_active = has_activity(w_id)
    if d_active and w_active:
        return (JOIN_BOTH_ACTIVE,
                "Both of those accounts have their own balance and history. "
                "Combining them means deciding which crafts, contracts and coins "
                "survive, so a moderator has to do it — open a ticket.", "")

    # Ties and the everyday case both go to Discord: it is the side that owns a
    # corp channel, guild roles and any KSP link, none of which move.
    keep, drop = (d_id, w_id) if (d_active or not w_active) else (w_id, d_id)
    keep_acct, drop_acct = (d_acct, w_acct) if keep == d_id else (w_acct, d_acct)

    # Move the dropped side's way of signing in onto the survivor.
    if keep == d_id:
        fuid = str(drop_acct.get("firebase_uid") or "")
        if not fuid:
            return JOIN_ERROR, "That website account has no sign-in to move.", ""
        code, message = link_firebase(keep, fuid,
                                      email=str(drop_acct.get("email") or ""))
    else:
        did = str(drop_acct.get("discord_id") or drop_id_of(drop_acct, drop))
        code, message = link_discord(keep, did)
    if code not in (LINK_OK, LINK_ALREADY):
        return JOIN_ERROR, message, ""

    # The username. One account, one name — so the survivor's stands, and the
    # other reservation is freed rather than left pointing at a deleted account.
    # A survivor with no name of its own inherits, which is what stops a player
    # who onboarded on the website from losing the name they just chose.
    kept_name = str(keep_acct.get("username") or "")
    drop_name = str(drop_acct.get("username") or "")
    if not kept_name and drop_name:
        try:
            _usernames().document(normalize_username(drop_name)).set(
                {"account_id": keep, "claimed_at": _now()})
            _accounts().document(keep).set({"username": drop_name}, merge=True)
            kept_name = drop_name
        except Exception as exc:
            log.warning("join: could not move username %r to %s: %s",
                        drop_name, keep, exc)

    # Carry across anything the survivor simply hasn't got.
    patch = {}
    for field in ("email", "avatar_url"):
        if not keep_acct.get(field) and drop_acct.get(field):
            patch[field] = drop_acct[field]
    if patch:
        try:
            _accounts().document(keep).set(patch, merge=True)
        except Exception as exc:
            log.warning("join: could not copy %s to %s: %s", list(patch), keep, exc)

    # The drained account goes, including its empty wallet — `delete_account`
    # only frees index rows and reservations that still point at it, so the ones
    # just moved above survive by construction.
    # END THE DROPPED ACCOUNT'S SESSIONS BEFORE DELETING IT.
    #
    # This is not tidying — it is the difference between the join working and the
    # player being stranded. Their browser is holding a 30-day token that says
    # `uid = <drop>`, and nothing about deleting a document reaches into a cookie.
    # Left alone, every request keeps arriving as the account that no longer
    # exists: `store.get_user` helpfully creates a fresh empty record, so the site
    # shows a real-looking session with 0 coins, no profile, and no admin rights,
    # and the join appears to have destroyed their account.
    #
    # Revoking forces one 401. The next sign-in resolves the same Google identity
    # through the index row moved above and lands on the surviving account, which
    # is the whole point. Ordered before the delete for the same reason the admin
    # delete path is: no window where the record is gone but a token still
    # resolves to it.
    try:
        from api_auth import logout_all_devices
        logout_all_devices(drop)
    except Exception as exc:
        log.warning("join: could not revoke sessions for %s: %s", drop, exc)

    # `delete_account` only frees index rows and reservations that STILL point at
    # the dropped account, so everything moved above survives by construction. The
    # dropped wallet is deliberately left: it is a default record by definition
    # (that is what `has_activity` just established), so there is nothing in it to
    # clean up and nothing to lose if this is ever re-run.
    delete_account(drop)

    log.warning("Joined accounts: kept %s, dropped %s", keep, drop)
    named = f" You're **{kept_name}**." if kept_name else ""
    return JOIN_OK, f"Accounts joined.{named}", keep


def drop_id_of(acct: dict, fallback: str) -> str:
    """The Discord id an account document represents (its own id, when it is a
    Discord account). Small helper so `join_accounts` reads straight through."""
    did = str(acct.get("discord_id") or "")
    return did or str(fallback)
