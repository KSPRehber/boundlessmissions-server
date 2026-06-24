"""
data/marketplace.py – Firestore + Firebase Storage helpers for the craft marketplace.

A listing is a craft (.craft blueprint) a player put up for sale. Listings are
non-exclusive: buying one transfers KCoins to the seller and DMs the buyer a copy
of the blueprint, but the listing stays active so anyone else can buy it too.

Firestore structure (GLOBAL — the marketplace spans every server):
    marketplace/{listing_id} → { ...listing fields..., guild_id (origin), mirrors }
"""
import logging
import uuid
from datetime import datetime
from typing import Any

from data.store import _db, _storage_bucket, safe_filename

log = logging.getLogger(__name__)

# Status constants
ACTIVE = "active"
DELISTED = "delisted"

ListingData = dict[str, Any]


def _col():
    """The single global marketplace collection (listings are visible/buyable in
    every server). guild_id is recorded on the doc as the origin only."""
    return _db.collection("marketplace")


def create_listing(
    guild_id: int, seller_id: int, seller_name: str,
    craft_name: str, craft_type: str, part_count: int,
    mass: float, cost: float, price: int,
    craft_url: str, craft_filename: str,
    blueprint_url: str = "",
    thumbnail_url: str = "",
    mods: list[str] | None = None,
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
        # Square NW-view render shown on the website's listing cards (the full
        # multi-view blueprint_url is reserved for the detail view). Empty for
        # listings made before the thumbnail existed — the site falls back to the
        # blueprint there.
        "thumbnail_url": thumbnail_url,
        # Distinct GameData mod folders the craft uses, sent by the KSP client at
        # list-time. Empty for stock-only crafts or listings made before mod tagging
        # existed. Powers the website's "filter by mod" facet.
        "mods": mods or [],
        "status": ACTIVE,
        "created_at": now,
        # Cross-server message mirrors: [{guild_id, channel_id, message_id}, ...]
        "mirrors": [],
        "buyers": [],
        "sales_count": 0,
    }
    _col().document(lid).set(doc)
    log.info("Listing %s created: %s selling %s for %d", lid, seller_name, craft_name, price)
    return doc


def get_listing(guild_id: int, listing_id: str) -> ListingData | None:
    snap = _col().document(listing_id).get()
    return snap.to_dict() if snap.exists else None


def update_listing(guild_id: int, listing_id: str, **fields) -> None:
    _col().document(listing_id).update(fields)


def list_active(guild_id: int) -> list[ListingData]:
    """All active listings, globally (guild_id ignored — one shared market)."""
    return [
        doc.to_dict()
        for doc in _col().where("status", "==", ACTIVE).stream()
    ]


def list_by_seller(seller_id: int) -> list[ListingData]:
    """Every listing a user created (active AND delisted) — the website's
    "My Uploads" view, where the seller can still see and delist their crafts."""
    return [
        doc.to_dict()
        for doc in _col().where("seller_id", "==", str(seller_id)).stream()
    ]


def list_by_buyer(buyer_id: int) -> list[ListingData]:
    """Every listing a user has bought (so the website can offer a free
    re-download under "My Purchases"). Firestore has no "array contains" index
    requirement issue here — it's a single array-contains on `buyers`."""
    return [
        doc.to_dict()
        for doc in _col().where("buyers", "array_contains", str(buyer_id)).stream()
    ]


def delete_listing(listing_id: str) -> None:
    """Permanently remove a listing: its Storage files (craft + blueprint, the whole
    marketplace/{id}/ prefix) and the Firestore document. Best-effort on Storage so a
    missing bucket/file never blocks deleting the record. Irreversible."""
    if _storage_bucket is not None:
        try:
            for blob in _storage_bucket.list_blobs(prefix=f"marketplace/{listing_id}/"):
                try:
                    blob.delete()
                except Exception as exc:
                    log.warning("Could not delete blob %s: %s", blob.name, exc)
        except Exception as exc:
            log.warning("Could not list Storage blobs for listing %s: %s", listing_id, exc)
    _col().document(listing_id).delete()
    log.info("Listing %s permanently deleted", listing_id)


def record_purchase(guild_id: int, listing_id: str, buyer_id: int) -> None:
    """Append a buyer and bump the sales counter."""
    doc_ref = _col().document(listing_id)
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
    path = f"marketplace/{listing_id}/{safe_filename(filename, 'craft.craft')}"
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


async def upload_thumbnail(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload the square NW-view thumbnail for a listing (website card). Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/thumbnail.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url
