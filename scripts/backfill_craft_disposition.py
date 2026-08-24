"""
One-off backfill: set `Content-Disposition: attachment` on every marketplace
craft blob already in Storage.

New uploads get the header automatically (see data/marketplace.upload_craft), but
craft listed before that fix still render as inline text when downloaded from the
website. Run this once to patch the existing objects:

    cd "GK Discord Bot"
    source .venv/bin/activate
    python -m scripts.backfill_craft_disposition

It only touches the `.craft` objects under marketplace/*/ — blueprints and
thumbnails (real images) are left alone.
"""
import logging

from data.store import _storage_bucket

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill")


def main() -> None:
    if _storage_bucket is None:
        raise SystemExit("Firebase Storage not configured (check FIREBASE_CREDENTIALS).")

    patched = skipped = 0
    for blob in _storage_bucket.list_blobs(prefix="marketplace/"):
        if not blob.name.lower().endswith(".craft"):
            continue
        filename = blob.name.rsplit("/", 1)[-1]
        want = f'attachment; filename="{filename}"'
        if blob.content_disposition == want:
            skipped += 1
            continue
        blob.content_disposition = want
        blob.patch()  # metadata-only update; doesn't re-upload the bytes
        patched += 1
        log.info("patched %s", blob.name)

    log.info("done: %d patched, %d already correct", patched, skipped)


if __name__ == "__main__":
    main()
