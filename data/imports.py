"""
data/imports.py – Per-user "craft import" queue.

When a player selects a craft for import in Discord (a completed bot-contract
craft via /library, or a craft they bought on the marketplace), an entry is
written here under that player's account. The KSP mod polls the pending queue
(see api_server.py /api/v1/craft/imports/...) and auto-imports each craft into
the active save, then acks it so the entry is deleted.

Storage layout: guilds/{gid}/ksp_craft_imports/{uid}/items/{import_id}
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from data.store import _db

log = logging.getLogger(__name__)

ImportEntry = dict[str, Any]


def _col(guild_id: int, user_id: int):
    return (_db.collection("guilds").document(str(guild_id))
            .collection("ksp_craft_imports").document(str(user_id))
            .collection("items"))


def enqueue(
    guild_id: int, user_id: int, source: str, ref_id: str, craft_name: str,
    vessel_node_url: str | None = None,
    craft_url: str | None = None,
    craft_filename: str | None = None,
    loadmeta: str | None = None,
    owner_name: str | None = None,
    flag_url: str | None = None,
) -> ImportEntry:
    """Queue a craft for the player's KSP client to auto-import.

    `source` is "contract", "market", "rescue_delivery", or "flag"; `ref_id` is
    the contract_id or listing_id. "contract"/"market" deliver a .craft blueprint
    (installed to the Ships folder); "rescue_delivery" carries a vessel_node_url
    and is imported as a LIVE vessel (the rescued craft, spawned in-save); "flag"
    carries a flag_url (PNG) installed into the KSP Flags dir — never a
    craft/vessel. If an identical entry is already queued (same source + ref_id)
    the existing entry is returned instead of creating a duplicate.
    """
    for doc in _col(guild_id, user_id).stream():
        d = doc.to_dict()
        if d.get("source") == source and d.get("ref_id") == ref_id:
            return d

    iid = uuid.uuid4().hex[:12]
    entry: ImportEntry = {
        "import_id": iid,
        "source": source,
        "ref_id": ref_id,
        "craft_name": craft_name,
        "vessel_node_url": vessel_node_url,
        "craft_url": craft_url,
        "craft_filename": craft_filename,
        "loadmeta": loadmeta,
        "owner_name": owner_name,
        "flag_url": flag_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _col(guild_id, user_id).document(iid).set(entry)
    log.info("Queued craft import %s (%s:%s) for user %d", iid, source, ref_id, user_id)
    return entry


def list_pending(guild_id: int, user_id: int) -> list[ImportEntry]:
    return [doc.to_dict() for doc in _col(guild_id, user_id).stream()]


def delete(guild_id: int, user_id: int, import_id: str) -> bool:
    ref = _col(guild_id, user_id).document(import_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True
