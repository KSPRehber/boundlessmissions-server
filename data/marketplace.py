"""
data/marketplace.py – Firestore + Firebase Storage helpers for the craft marketplace.

A listing is a craft (.craft blueprint) a player put up for sale. Listings are
non-exclusive: buying one transfers KCoins to the seller and DMs the buyer a copy
of the blueprint, but the listing stays active so anyone else can buy it too.

Firestore structure:
    guilds/{guild_id}/marketplace/{listing_id} → { ...listing fields... }
"""
import logging
import uuid
from datetime import datetime
from typing import Any

from data.store import _db, _storage_bucket

log = logging.getLogger(__name__)

# Status constants
ACTIVE = "active"
DELISTED = "delisted"

ListingData = dict[str, Any]


def _col(guild_id: int):
    return _db.collection("guilds").document(str(guild_id)).collection("marketplace")


def create_listing(
    guild_id: int, seller_id: int, seller_name: str,
    craft_name: str, craft_type: str, part_count: int,
    mass: float, cost: float, price: int,
    craft_url: str, craft_filename: str,
    blueprint_url: str = "",
) -> ListingData:
    lid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: ListingData = {
        "listing_id": lid,
        "guild_id": str(guild_id),
        "seller_id": str(seller_id),
        "seller_name": seller_name,
        "craft_name": craft_name,
        "craft_type": craft_type,
        "part_count": part_count,
        "mass": mass,
        "cost": cost,
        "price": price,
        "craft_url": craft_url,
        "craft_filename": craft_filename,
        "blueprint_url": blueprint_url,
        "status": ACTIVE,
        "created_at": now,
        "channel_msg_id": None,
        "buyers": [],
        "sales_count": 0,
    }
    _col(guild_id).document(lid).set(doc)
    log.info("Listing %s created: %s selling %s for %d", lid, seller_name, craft_name, price)
    return doc


def get_listing(guild_id: int, listing_id: str) -> ListingData | None:
    snap = _col(guild_id).document(listing_id).get()
    return snap.to_dict() if snap.exists else None


def update_listing(guild_id: int, listing_id: str, **fields) -> None:
    _col(guild_id).document(listing_id).update(fields)


def list_active(guild_id: int) -> list[ListingData]:
    return [
        doc.to_dict()
        for doc in _col(guild_id).where("status", "==", ACTIVE).stream()
    ]


def record_purchase(guild_id: int, listing_id: str, buyer_id: int) -> None:
    """Append a buyer and bump the sales counter."""
    doc_ref = _col(guild_id).document(listing_id)
    snap = doc_ref.get()
    if not snap.exists:
        return
    data = snap.to_dict()
    buyers = data.get("buyers", [])
    if str(buyer_id) not in buyers:
        buyers.append(str(buyer_id))
    doc_ref.update({
        "buyers": buyers,
        "sales_count": data.get("sales_count", 0) + 1,
    })


async def upload_craft(listing_id: str, filename: str, data: bytes) -> str:
    """Upload a raw (decompressed) .craft file to Storage. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/{filename}"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type="text/plain")
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def upload_blueprint(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload a rendered blueprint image for a listing. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/blueprint.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url
