"""
data/contracts.py – Firestore + Firebase Storage helpers for contracts.

Firestore structure:
    contracts/{contract_id}          → { ...contract fields... }
    contract_reports/{contract_id}_{reporter_id} → { ...one report... }
"""
import logging
import uuid
from datetime import datetime
from typing import Any

import aiohttp
from firebase_admin import firestore

from data.store import (
    _db, _storage_bucket, safe_filename, safe_content_type,
    upload_private, signed_url, sign_stored, is_storage_path,
    SIGNED_URL_MAX_TTL,
)

log = logging.getLogger(__name__)

# Status constants
PENDING = "pending"
ACTIVE = "active"
SUBMITTED = "submitted"
COMPLETED = "completed"
DISPUTED = "disputed"
MOD_REVIEW = "mod_review"
CANCELLED = "cancelled"

# Mission-type values (stored in the contract's "mission_type" field)
CRAFT_BUILD = "craft_build"
ACTIVE_VESSEL = "active_vessel"
RESCUE = "rescue"
FLAG_DESIGN = "flag_design"

ContractData = dict[str, Any]


def _col(guild_id: int = 0):
    """The single GLOBAL contracts collection — a contract can run between users in
    different servers. guild_id is accepted for call-site compatibility but is only
    stored on the doc as the origin guild (used for channel routing)."""
    return _db.collection("contracts")


def create_contract(
    guild_id: int, issuer_id: int, issuer_name: str,
    contractor_id: int, contractor_name: str,
    mission: str, payment: int, fine: int, due_date: str,
    modlist: str | None = None,
    *,
    mission_type: str | None = None,
    rescue_target: dict | None = None,
    rescue_vessel_node_url: str | None = None,
    rescue_kerbals: list | None = None,
    rescue_pid: str | None = None,
    life_support: str | None = None,
    ls_endurance_days: float = 0.0,
    ls_crew_capacity: int = 0,
) -> ContractData:
    cid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: ContractData = {
        "contract_id": cid,
        "guild_id": str(guild_id),
        "issuer_id": str(issuer_id),
        "issuer_name": issuer_name,
        "contractor_id": str(contractor_id),
        "contractor_name": contractor_name,
        "mission": mission,
        "payment": payment,
        "fine": fine,
        "due_date": due_date,
        "status": PENDING,
        "created_at": now,
        "submitted_at": None,
        "completed_at": None,
        "submitted_files": [],
        "dm_message_id": None,
        "issuer_review_msg_id": None,
        "modlist": modlist,
    }
    # Rescue-mission fields. The issuer's snapshotted vessel (the wreck the
    # rescuer recovers) is removed from the issuer's save at creation time and
    # restored if the contract never completes. rescue_kerbals are the tagged
    # names ("{issuer}'s {kerbal}") the rescuer must recover.
    if mission_type:
        doc["mission_type"] = mission_type
    if mission_type == RESCUE:
        doc["rescue_target"] = rescue_target
        doc["rescue_vessel_node_url"] = rescue_vessel_node_url
        doc["rescue_kerbals"] = rescue_kerbals or []
        doc["rescue_pid"] = rescue_pid
        doc["issuer_vessel_removed"] = True
        doc["delivered_vessel_node_url"] = None
    # Life-support provisioning of the craft this contract is about. On a rescue it is
    # known at creation (the wreck already exists and was scanned); on a normal contract
    # it arrives with the submitted craft instead, and is written then.
    if life_support and life_support.lower() != "none":
        doc["life_support"] = life_support.lower()
        doc["ls_endurance_days"] = float(ls_endurance_days or 0.0)
        doc["ls_crew_capacity"] = int(ls_crew_capacity or 0)
    _col(guild_id).document(cid).set(doc)
    log.info("Contract %s created: %s -> %s (%d coins)", cid, issuer_name, contractor_name, payment)
    return doc


def get_contract(guild_id: int, contract_id: str) -> ContractData | None:
    snap = _col(guild_id).document(contract_id).get()
    return snap.to_dict() if snap.exists else None


def update_contract(guild_id: int, contract_id: str, **fields) -> None:
    _col(guild_id).document(contract_id).update(fields)


def claim_submission(guild_id: int, contract_id: str, fields: dict[str, Any]) -> bool:
    """Atomically move a contract ACTIVE -> SUBMITTED, writing `fields` with it.

    Returns True only for the call that did the flip. `submit_contract` awaits real
    I/O (file reads, the ban check, Storage uploads) between reading `status ==
    ACTIVE` and writing SUBMITTED, and the transitions in `contract_actions`
    (cancel, give_up, ...) can run in that window from another request. A plain
    `update()` would then clobber a CANCELLED contract — whose escrow was already
    refunded — back to SUBMITTED, and the review that followed would pay the
    contractor from nothing. Deciding the flip inside a Firestore transaction
    makes the status check and the write one step, which holds even with no
    shared in-process lock and across workers (the `try_claim_purchase` pattern).
    """
    ref = _col(guild_id).document(contract_id)
    transaction = _db.transaction()
    fields = dict(fields)
    fields["status"] = SUBMITTED

    @firestore.transactional
    def _claim(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or (snap.to_dict() or {}).get("status") != ACTIVE:
            return False
        txn.update(ref, fields)
        return True

    return _claim(transaction)


def delete_stored_file(path: str) -> bool:
    """Best-effort delete of a Storage object stored on a contract by bucket path
    (the shape `upload_private_to_storage` returns). False if nothing was deleted."""
    if _storage_bucket is None or not path or "://" in str(path):
        return False
    try:
        _storage_bucket.blob(str(path)).delete()
        return True
    except Exception as exc:
        log.warning("Could not delete stored file %s: %s", path, exc)
        return False


def iter_user_contracts(guild_id: int, user_id: int) -> list[ContractData]:
    """All contracts where the user is issuer or contractor, deduped by id.

    Uses two single-field-equality queries (each served by Firestore's automatic
    single-field index — no composite index required) instead of streaming every
    contract in the guild and OR-filtering in Python. The returned set is
    identical to the old `where("status","in",...).stream()` + Python filter,
    minus the status filter, which callers apply in-memory.
    """
    uid = str(user_id)
    col = _col(guild_id)
    by_id: dict[str, ContractData] = {}
    for field in ("contractor_id", "issuer_id"):
        for doc in col.where(field, "==", uid).stream():
            by_id[doc.id] = doc.to_dict()
    return list(by_id.values())


def list_by_status(status: str) -> list[ContractData]:
    """Every contract in a given status, across all guilds.

    Served by Firestore's automatic single-field index, so this is a bounded query
    rather than a full-collection scan — which matters because the dispute-timeout
    sweep runs on a timer forever. Contracts are global (see _col), hence no guild_id.
    """
    return [doc.to_dict() for doc in _col().where("status", "==", status).stream()]


def count_active(guild_id: int, user_id: int) -> int:
    active_statuses = {PENDING, ACTIVE, SUBMITTED, DISPUTED, MOD_REVIEW}
    return sum(
        1 for c in iter_user_contracts(guild_id, user_id)
        if c.get("status") in active_statuses
    )


# ── Reports ──────────────────────────────────────────────────────────────────
#
# A contract report is the marketplace's report system pointed at the other side of
# a deal (see data/marketplace.py). The shape is deliberately identical — a keyed
# (subject, reporter) document plus a counter on the subject — because the question
# a moderator asks is the same one: has this person been reported before, and by how
# many different people?
#
# What differs is who may file one. A listing is public and anyone browsing can
# report it; a contract is private to its two parties, so the only people who can
# see one to complain about it are the issuer and the contractor. The endpoint
# enforces that; this module only stores what it is given.


def _report_id(contract_id: str, reporter_id: int | str) -> str:
    return f"{contract_id}_{reporter_id}"


def get_report(contract_id: str, reporter_id: int | str) -> dict[str, Any] | None:
    """This user's existing report against this contract, if any.

    The document id is the (contract, reporter) pair, so "have I already reported
    this?" is one keyed read — no composite index, and no way to file the same
    complaint twice to make it look louder."""
    snap = _db.collection("contract_reports").document(
        _report_id(contract_id, reporter_id)).get()
    return snap.to_dict() if snap.exists else None


def record_report(contract: ContractData, reporter_id: int | str, reporter_name: str,
                  reason: str, guild_id: int | str = "",
                  ticket_channel_id: int | str = "") -> None:
    """Store a report and bump the contract's report_count.

    The Discord ticket is where a report is actually *handled*; this record exists so
    the count survives the ticket being closed, and so a second report from the same
    user overwrites rather than accumulates.

    The contract's status at the time is stored with it, because a report is about a
    moment — "they refused after I delivered" reads very differently once the same
    contract has been settled — and the live document will have moved on by the time
    a moderator opens the ticket.
    """
    contract_id = contract["contract_id"]
    reporter = str(reporter_id)
    issuer_id = str(contract.get("issuer_id", ""))
    # Whoever the reporter is not. A report is always *about* the counterparty, so
    # storing them saves every reader re-deriving it from two ids and a role.
    subject_id = (str(contract.get("contractor_id", "")) if reporter == issuer_id
                  else issuer_id)
    subject_name = (contract.get("contractor_name", "") if reporter == issuer_id
                    else contract.get("issuer_name", ""))
    first_time = get_report(contract_id, reporter_id) is None
    _db.collection("contract_reports").document(_report_id(contract_id, reporter_id)).set({
        "contract_id": contract_id,
        "mission": (contract.get("mission", "") or "")[:500],
        "status": contract.get("status", ""),
        "issuer_id": issuer_id,
        "issuer_name": contract.get("issuer_name", ""),
        "contractor_id": str(contract.get("contractor_id", "")),
        "contractor_name": contract.get("contractor_name", ""),
        "subject_id": subject_id,
        "subject_name": subject_name,
        "reporter_id": reporter,
        "reporter_name": reporter_name,
        "reason": reason,
        "guild_id": str(guild_id),
        "ticket_channel_id": str(ticket_channel_id),
        "created_at": datetime.utcnow().isoformat(),
    })
    if first_time:
        try:
            _col().document(contract_id).update({"report_count": firestore.Increment(1)})
        except Exception as exc:  # a vanished contract must not lose the report
            log.warning("Could not bump report_count for contract %s: %s", contract_id, exc)
    log.info("Contract %s reported by %s (%s)", contract_id, reporter_name, reporter_id)


async def upload_to_storage(contract_id: str, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload a file to Firebase Storage under contracts/{contract_id}/. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    # Sanitize the client-supplied filename + content type before they reach the
    # public object path (no prefix escape / sibling shadowing / active-content).
    path = f"contracts/{contract_id}/{safe_filename(filename, 'file')}"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type))
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def upload_private_to_storage(contract_id: str, filename: str, data: bytes,
                                    content_type: str = "application/octet-stream") -> str:
    """Upload a file under contracts/{contract_id}/ as a PRIVATE object and return
    its bucket path (not a URL). Same path convention and sanitization as
    upload_to_storage, but never made public — the craft/vessel file "private to the
    two parties" is served only through a signed URL minted at request time. Callers
    store the returned path on the contract; serve points run it through
    store.sign_stored()."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"contracts/{contract_id}/{safe_filename(filename, 'file')}"
    return upload_private(path, data, content_type)


async def download_url(url: str) -> bytes:
    # A stored reference may be a bare bucket path (a private object) rather than a
    # URL — resolve it to a signed URL first, so every internal reader works
    # regardless of which storage scheme produced the reference.
    if is_storage_path(url):
        url = signed_url(url)
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()
