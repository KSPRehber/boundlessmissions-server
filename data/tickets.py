"""
data/tickets.py – a ticket as a record, with the Discord channel as its projection.

A ticket used to BE a Discord channel. Its state lived in the channel topic
(`GKTicket|opener=…|kind=…`) and the only thing in Firestore was a counter, which
meant there was nothing to list, nothing to read back, and no way to know a ticket
was yours except by being able to see the channel. Fine while every user was a
Discord user; useless the moment someone has an account and no server to open it in.

So the record here is the ticket, and the channel is a projection of it — the same
inversion the corps needed. Two consequences worth stating:

  • **The channel can be absent.** A website-only player's ticket has no
    `channel_id` until (and unless) one is made. Nothing here requires one.
  • **Messages flow both ways.** A mod replying in the channel and a player
    replying on the website write the same kind of document, distinguished by
    `author_kind`, so one thread reads correctly from either end.

Mirrored Discord messages carry `discord_message_id`, which is what stops the
listener echoing a message the website just posted *into* Discord back out again.
"""

import logging
import uuid
from datetime import datetime, timezone

from firebase_admin import firestore

from data.store import _db

log = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"

# Who wrote a message. The distinction the UI actually draws is "me" vs "the
# team", and `staff` covers a mod in the channel whether or not they have an
# account here at all.
AUTHOR_OPENER = "opener"
AUTHOR_STAFF = "staff"
AUTHOR_SYSTEM = "system"

# A thread is a conversation, not a log. This caps what one ticket can accumulate
# so a runaway loop (or a bored user) cannot grow a document set without bound.
MAX_MESSAGES_RETURNED = 200


def _col():
    return _db.collection("tickets")


def _messages(ticket_id: str):
    return _col().document(str(ticket_id)).collection("messages")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Tickets ──────────────────────────────────────────────────────────────────

def create(*, guild_id, opener_id, kind: str, title: str, description: str = "",
           number: int = 0, channel_id=None, subject_user_id=None) -> dict | None:
    """Record a new ticket. Returns the document, or None if it could not be written.

    `opener_id` is an ACCOUNT id, not a Discord id — that is the whole point: the
    person who can read this ticket on the website is identified the same way
    everywhere else in the system.
    """
    ticket_id = uuid.uuid4().hex[:16]
    doc = {
        "ticket_id": ticket_id,
        "number": int(number or 0),
        "guild_id": str(guild_id or ""),
        "opener_id": str(opener_id or ""),
        "kind": str(kind or "other"),
        "title": str(title or "").strip()[:200],
        "description": str(description or "").strip()[:4000],
        "status": OPEN,
        "channel_id": str(channel_id or ""),
        "subject_user_id": str(subject_user_id or ""),
        "created_at": _now(),
        "updated_at": _now(),
        "closed_at": "",
        "closed_by": "",
        # Drives the badge on the account page. Set when the team replies, cleared
        # when the opener reads the thread.
        "unread_for_opener": False,
        "message_count": 0,
    }
    try:
        _col().document(ticket_id).set(doc)
        log.info("Recorded ticket %s (#%s, kind=%s) for %s",
                 ticket_id, number, kind, opener_id)
        return doc
    except Exception as exc:
        log.warning("Could not record ticket for %s: %s", opener_id, exc)
        return None


def get(ticket_id: str) -> dict | None:
    try:
        snap = _col().document(str(ticket_id)).get()
    except Exception as exc:
        log.warning("Could not read ticket %s: %s", ticket_id, exc)
        return None
    return snap.to_dict() if snap.exists else None


def get_by_channel(channel_id) -> dict | None:
    """The ticket a Discord channel projects, or None.

    How the message listener knows a channel is a ticket at all. Queried on a
    single field so no composite index is needed.
    """
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = _col().where(filter=FieldFilter("channel_id", "==", str(channel_id))).limit(1)
        for doc in q.stream():
            return doc.to_dict()
    except Exception as exc:
        log.warning("Could not resolve channel %s to a ticket: %s", channel_id, exc)
    return None


def list_for_account(account_id, limit: int = 50) -> list[dict]:
    """Every ticket this account opened, newest first.

    Sorted in Python rather than with `order_by`: combining it with the equality
    filter would need a composite index, and a player has tens of tickets, not
    thousands.
    """
    out: list[dict] = []
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = _col().where(filter=FieldFilter("opener_id", "==", str(account_id)))
        out = [d.to_dict() for d in q.stream() if d.to_dict()]
    except Exception as exc:
        log.warning("Could not list tickets for %s: %s", account_id, exc)
        return []
    out.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    return out[:limit]


def set_channel(ticket_id: str, channel_id) -> None:
    try:
        _col().document(str(ticket_id)).set(
            {"channel_id": str(channel_id), "updated_at": _now()}, merge=True)
    except Exception as exc:
        log.warning("Could not attach channel to ticket %s: %s", ticket_id, exc)


def close(ticket_id: str, closed_by: str = "") -> bool:
    try:
        _col().document(str(ticket_id)).set({
            "status": CLOSED,
            "closed_at": _now(),
            "closed_by": str(closed_by or ""),
            "updated_at": _now(),
        }, merge=True)
        return True
    except Exception as exc:
        log.warning("Could not close ticket %s: %s", ticket_id, exc)
        return False


def mark_read(ticket_id: str) -> None:
    """The opener has looked at the thread."""
    try:
        _col().document(str(ticket_id)).set({"unread_for_opener": False}, merge=True)
    except Exception as exc:
        log.warning("Could not clear unread on ticket %s: %s", ticket_id, exc)


# ── Messages ─────────────────────────────────────────────────────────────────

def add_message(ticket_id: str, *, author_id, author_name: str, author_kind: str,
                body: str, discord_message_id=None,
                attachments: list[dict] | None = None) -> dict | None:
    """Append a message to a thread.

    `discord_message_id` is set for anything that originated in (or was mirrored
    to) Discord. The listener checks it before recording, which is what stops a
    website reply — posted into the channel by us — from being mirrored straight
    back in as a duplicate.
    """
    msg_id = uuid.uuid4().hex[:16]
    doc = {
        "message_id": msg_id,
        "author_id": str(author_id or ""),
        "author_name": str(author_name or "")[:80],
        "author_kind": str(author_kind or AUTHOR_SYSTEM),
        "body": str(body or "")[:4000],
        "attachments": attachments or [],
        "discord_message_id": str(discord_message_id or ""),
        "created_at": _now(),
    }
    try:
        _messages(ticket_id).document(msg_id).set(doc)
        # The counter and the unread flag move together with the write that
        # caused them, so a thread can never show a reply it has no record of.
        patch = {
            "updated_at": doc["created_at"],
            "message_count": firestore.Increment(1),
        }
        if author_kind == AUTHOR_STAFF:
            patch["unread_for_opener"] = True
        _col().document(str(ticket_id)).set(patch, merge=True)
        return doc
    except Exception as exc:
        log.warning("Could not add message to ticket %s: %s", ticket_id, exc)
        return None


def link_discord_message(ticket_id: str, message_id: str, discord_message_id) -> None:
    """Tie a thread message to the Discord copy that was posted for it.

    Written after the fact because the Discord id does not exist until the post
    succeeds. Without it a website reply — which the bot posts into the channel —
    comes back through the mirror listener as a second copy of itself.
    """
    try:
        _messages(ticket_id).document(str(message_id)).set(
            {"discord_message_id": str(discord_message_id)}, merge=True)
    except Exception as exc:
        log.warning("Could not link message %s on ticket %s: %s",
                    message_id, ticket_id, exc)


def has_discord_message(ticket_id: str, discord_message_id) -> bool:
    """Whether a Discord message is already in the thread.

    The listener's guard against double-recording: the website's own replies are
    posted into the channel by the bot, so they come back through `on_message`
    like any other, and without this each would appear twice.
    """
    if not discord_message_id:
        return False
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = _messages(ticket_id).where(
            filter=FieldFilter("discord_message_id", "==", str(discord_message_id))).limit(1)
        return any(True for _ in q.stream())
    except Exception as exc:
        log.warning("Could not check message %s on ticket %s: %s",
                    discord_message_id, ticket_id, exc)
        return False


def messages(ticket_id: str, limit: int = MAX_MESSAGES_RETURNED) -> list[dict]:
    """The thread, oldest first."""
    out: list[dict] = []
    try:
        out = [d.to_dict() for d in _messages(ticket_id).stream() if d.to_dict()]
    except Exception as exc:
        log.warning("Could not read messages for ticket %s: %s", ticket_id, exc)
        return []
    out.sort(key=lambda m: str(m.get("created_at") or ""))
    return out[-limit:]
