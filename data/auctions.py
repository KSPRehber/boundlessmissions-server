"""
data/auctions.py – Firestore helpers for reverse (Dutch) auctions.

An auction is a contract whose price is bid DOWN by contractors. The lowest bid
when the auction closes wins and is converted into an active contract.
Documents live in the GLOBAL auctions/{auction_id} collection (auctions are
mirrored into every server); guild_id on the doc is the origin only.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from firebase_admin import firestore

import settings
from data.store import _db

log = logging.getLogger(__name__)

# Status constants
OPEN = "open"
CLOSED = "closed"        # ended with a winner
CANCELLED = "cancelled"  # ended with no bids (escrow refunded)

AuctionData = dict[str, Any]


def _col():
    return _db.collection("auctions")


def create_auction(
    guild_id: int, issuer_id: int, issuer_name: str,
    mission: str, start_value: int, fine: int, due_date: str,
    ends_at: str, modlist: str | None = None, min_decrement: int = 1,
    mission_type: str | None = None,
) -> AuctionData:
    aid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: AuctionData = {
        "auction_id": aid,
        "guild_id": str(guild_id),
        "issuer_id": str(issuer_id),
        "issuer_name": issuer_name,
        "mission": mission,
        "start_value": start_value,
        # current_bid starts at the ceiling; bidder is None until someone bids.
        "current_bid": start_value,
        "current_bidder_id": None,
        "current_bidder_name": None,
        "bid_count": 0,
        "fine": fine,
        "due_date": due_date,
        "modlist": modlist,
        "min_decrement": min_decrement,
        "status": OPEN,
        "created_at": now,
        "ends_at": ends_at,
        # Cross-server message mirrors: [{guild_id, channel_id, message_id}, ...]
        "mirrors": [],
        "result_contract_id": None,
    }
    # Mission type (craft_build / active_vessel) the winner's contract inherits.
    if mission_type:
        doc["mission_type"] = mission_type
    _col().document(aid).set(doc)
    log.info("Auction %s created by %s (start %d, ends %s)", aid, issuer_name, start_value, ends_at)
    return doc


def get_auction(guild_id: int, auction_id: str) -> AuctionData | None:
    snap = _col().document(auction_id).get()
    return snap.to_dict() if snap.exists else None


def update_auction(guild_id: int, auction_id: str, **fields) -> None:
    _col().document(auction_id).update(fields)


def try_place_bid(guild_id: int, auction_id: str, bidder_id, bidder_name: str,
                  amount: int, antisnipe_seconds: int = 0) -> dict:
    """Atomically place a reverse-auction bid (lowest bid wins), re-validating
    against the *committed* auction state inside a Firestore transaction.

    A plain read-then-update lets two near-simultaneous bids validate against the
    same `current_bid` and then race to write, so the later write wins even when it
    is a higher (worse) bid — the earlier, genuinely-lower bid is silently clobbered.
    Doing the ceiling check and the write in one transaction closes that: the ceiling
    is measured against whatever bid is actually committed at write time.

    Returns one of:
      {"ok": True,  "auction": <updated dict>}
      {"ok": False, "reason": "missing"}
      {"ok": False, "reason": "closed"}
      {"ok": False, "reason": "own"}
      {"ok": False, "reason": "no_discord"}
      {"ok": False, "reason": "too_high", "ceiling": <int>}
      {"ok": False, "reason": "fine_cap", "floor": <int>, "fine": <int>, "mult": <int>}

    Auctions are a Discord game: the winner is handed a work view in their corp
    channel or DM, and `close_auction` needs a member to do that. A website-only
    account (`a_…` id) has no such surface, so its bid is refused here — inside the
    storage function rather than only at the web endpoint — because a bidder the
    closer cannot resolve used to make the auction unclosable and hold the issuer's
    escrow forever.
    """
    if not str(bidder_id).isdigit():
        return {"ok": False, "reason": "no_discord"}
    ref = _col().document(auction_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _bid(txn) -> dict:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return {"ok": False, "reason": "missing"}
        a = snap.to_dict() or {}

        now = datetime.utcnow()
        if a.get("status") != OPEN or a.get("ends_at", "") <= now.isoformat():
            return {"ok": False, "reason": "closed"}
        if str(bidder_id) == str(a.get("issuer_id")):
            return {"ok": False, "reason": "own"}

        # A bid is money the issuer will owe: zero or negative is not a lower bid,
        # it is a broken one. Both current callers already refuse it, so this is a
        # backstop — but it belongs in the transaction with the other invariants,
        # because `close_auction` computes the issuer's refund as
        # `start_value - final` and a negative `final` refunds more than was ever
        # escrowed, which mints coins.
        if amount <= 0:
            return {"ok": False, "reason": "too_high", "ceiling": 0, "step": 0}

        step = a.get("min_decrement", 1)
        ceiling = a["current_bid"] - step
        if amount > ceiling:
            return {"ok": False, "reason": "too_high", "ceiling": ceiling, "step": step}

        # MAX_FINE_MULTIPLE_OF_PAYMENT was checked when the auction opened, against
        # the START value — but the contract's payment is the WINNING bid. Start
        # 10 000 / fine 50 000 / bid 1 passed at open and bound the bidder to a
        # 1-coin job carrying a 50 000 fine, which a give-up or dispute timeout
        # turns into a debt that follows them; an issuer can bait that on purpose,
        # since the low bidder always "wins". So a bid may not go below the price
        # at which the fine would breach the cap. The floor is named in the
        # refusal: it is a number the bidder can act on.
        mult = settings.MAX_FINE_MULTIPLE_OF_PAYMENT
        fine = int(a.get("fine", 0) or 0)
        if mult > 0 and fine > 0 and fine > amount * mult:
            floor = -(-fine // mult)   # ceil(fine / mult)
            return {"ok": False, "reason": "fine_cap", "floor": floor, "fine": fine,
                    "mult": mult}

        fields = {
            "current_bid": amount,
            "current_bidder_id": str(bidder_id),
            "current_bidder_name": bidder_name,
            "bid_count": a.get("bid_count", 0) + 1,
        }
        # Anti-snipe: a late bid pushes the end back so others can respond. Evaluated
        # against the committed end time, same as the ceiling.
        if antisnipe_seconds > 0:
            end_dt = datetime.fromisoformat(a["ends_at"])
            if (end_dt - now).total_seconds() < antisnipe_seconds:
                fields["ends_at"] = (now + timedelta(seconds=antisnipe_seconds)).isoformat()

        txn.update(ref, fields)
        a.update(fields)
        return {"ok": True, "auction": a}

    return _bid(transaction)


def claim_close(auction_id: str) -> AuctionData | None:
    """Take the auction out of OPEN atomically, and hand back the state to close on.

    Two separate defects made this necessary, and one transaction closes both.

    **The bid/close race.** `_close_auction_locked` read the auction plainly and then
    spent several awaits — `contractor_gate`, `create_contract`, `add_balance` — before
    writing CLOSED. `try_place_bid` refuses a bid only once `ends_at` has passed, so on
    an EARLY close (the issuer's "End now" button, or the web end endpoint) a bid landing
    inside that window committed, told the bidder "you're the lowest bidder", and was
    then ignored: the close bound the PREVIOUS bidder at the PREVIOUS price, leaving the
    stored auction naming one player and its `result_contract_id` naming another.
    `try_place_bid` was made transactional for exactly this class of problem; the close
    it feeds was not. `_close_locks` only serialises closes against each other.

    **The retry.** The close moved money before writing the status, and the sweeper runs
    every 30 seconds. A failure at the status write left the auction OPEN with `ends_at`
    in the past, so the next tick created a SECOND contract binding the same winner and
    paid a SECOND refund.

    So the status leaves OPEN inside the transaction, before any of that work: a
    concurrent bid then fails its own `status != OPEN` check, and a retry finds nothing
    to claim. The frozen `current_bid` / `current_bidder_id` in the returned dict are
    what the caller must use — re-reading the document afterwards would reintroduce the
    race this removes.

    CLOSED is written provisionally because the branch (bound / refused / no bids) is
    decided from the very values this transaction freezes; the caller downgrades it to
    CANCELLED when there is no winner or the winner cannot be bound. A crash between the
    claim and that downgrade leaves a CLOSED auction with no `result_contract_id`, which
    is visible and repairable — and, unlike the state it replaces, does not duplicate a
    contract or a refund. That is the same direction the contract transitions chose.

    Returns the frozen auction, or None when it was not OPEN (already closed, already
    claimed, or gone) — in which case the caller must do nothing at all.
    """
    ref = _col().document(auction_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _claim(txn) -> AuctionData | None:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None
        a = snap.to_dict() or {}
        if a.get("status") != OPEN:
            return None
        txn.update(ref, {"status": CLOSED, "close_claimed_at": datetime.utcnow().isoformat()})
        a["status"] = CLOSED
        return a

    return _claim(transaction)


def list_open(guild_id: int) -> list[AuctionData]:
    """All open auctions, globally (guild_id ignored; used by the close loop)."""
    return [d.to_dict() for d in _col().where("status", "==", OPEN).stream()]
