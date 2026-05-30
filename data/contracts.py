"""
data/contracts.py – Firestore + Firebase Storage helpers for contracts.
"""
import logging
import uuid
from datetime import datetime
from typing import Any

import aiohttp

from data.store import _db, _storage_bucket

log = logging.getLogger(__name__)

# Status constants
PENDING = "pending"
ACTIVE = "active"
SUBMITTED = "submitted"
COMPLETED = "completed"
DISPUTED = "disputed"
MOD_REVIEW = "mod_review"
CANCELLED = "cancelled"

ContractData = dict[str, Any]


def _col(guild_id: int):
    return _db.collection("guilds").document(str(guild_id)).collection("contracts")


def create_contract(
    guild_id: int, issuer_id: int, issuer_name: str,
    contractor_id: int, contractor_name: str,
    mission: str, payment: int, fine: int, due_date: str,
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
    }
    _col(guild_id).document(cid).set(doc)
    log.info("Contract %s created: %s -> %s (%d coins)", cid, issuer_name, contractor_name, payment)
    return doc


def get_contract(guild_id: int, contract_id: str) -> ContractData | None:
    snap = _col(guild_id).document(contract_id).get()
    return snap.to_dict() if snap.exists else None


def update_contract(guild_id: int, contract_id: str, **fields) -> None:
    _col(guild_id).document(contract_id).update(fields)


def count_active(guild_id: int, user_id: int) -> int:
    col = _col(guild_id)
    active_statuses = [PENDING, ACTIVE, SUBMITTED, DISPUTED, MOD_REVIEW]
    count = 0
    for doc in col.where("status", "in", active_statuses).stream():
        d = doc.to_dict()
        if d.get("issuer_id") == str(user_id) or d.get("contractor_id") == str(user_id):
            count += 1
    return count


async def upload_to_storage(contract_id: str, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload a file to Firebase Storage under contracts/{contract_id}/. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"contracts/{contract_id}/{filename}"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def download_url(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()
