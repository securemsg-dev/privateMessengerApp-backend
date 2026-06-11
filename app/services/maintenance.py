from __future__ import annotations
"""
app/services/maintenance.py
────────────────────────────
Periodic background housekeeping, run from the app lifespan (one loop per
process — the work is idempotent, so multiple instances sweeping is safe):

  • Abandoned media blobs — rows reserved via POST /media/upload-url whose
    bytes were never PUT (`uploaded_at IS NULL`) older than 24h. Both the
    DB row and any stray ciphertext file are removed.

  • Expired sessions — refresh-token rows past `expires_at`. They are
    already rejected by auth; this just stops the table growing unbounded.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.db.models.media_blob import MediaBlob
from app.db.models.session import Session
from app.db.session import AsyncSessionLocal
from app.services.media_storage import get_storage

logger = logging.getLogger(__name__)

ABANDONED_BLOB_MAX_AGE = timedelta(hours=24)
SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly


async def sweep_once() -> tuple[int, int]:
    """Run one maintenance pass. Returns (blobs_removed, sessions_removed)."""
    now = datetime.now(timezone.utc)
    storage = get_storage()

    async with AsyncSessionLocal() as db:
        # ── Abandoned media blobs ────────────────────────────────────────
        cutoff = now - ABANDONED_BLOB_MAX_AGE
        abandoned = (await db.execute(
            select(MediaBlob).where(
                MediaBlob.uploaded_at.is_(None),
                MediaBlob.created_at < cutoff,
            )
        )).scalars().all()

        for blob in abandoned:
            await storage.delete_bytes(blob.id)
            await db.delete(blob)

        # ── Expired sessions ─────────────────────────────────────────────
        expired = await db.execute(
            delete(Session).where(Session.expires_at < now)
        )

        await db.commit()

    return len(abandoned), expired.rowcount or 0


async def run_maintenance_loop(
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep forever until the task is cancelled at shutdown."""
    while True:
        try:
            blobs, sessions = await sweep_once()
            if blobs or sessions:
                logger.info(
                    "Maintenance sweep: removed %d abandoned blob(s), "
                    "%d expired session(s)",
                    blobs, sessions,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a bad sweep kill the loop — try again next interval.
            logger.exception("Maintenance sweep failed")
        await asyncio.sleep(interval_seconds)
