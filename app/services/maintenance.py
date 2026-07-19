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

  • Stale calls — rows with no `ended_at` long after they started (client
    crashed mid-setup or never sent the hang-up). Closed so the Calls tab
    doesn't show "In progress" forever.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.db.models.call import Call
from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from app.db.models.session import Session
from app.db.models.user import User
from app.db.session import AsyncSessionLocal
from app.services.media_storage import get_storage

logger = logging.getLogger(__name__)

ABANDONED_BLOB_MAX_AGE = timedelta(hours=24)
STALE_CALL_MAX_AGE = timedelta(hours=2)
# Uploaded blobs referenced by NO message and NO avatar. Deliberately long:
# messages sent by pre-media_blob_id clients carry no plaintext reference, so
# their attachments look unreferenced — a long grace period keeps that media
# alive until those clients are retired, while still bounding disk growth.
UNREFERENCED_BLOB_MAX_AGE = timedelta(days=30)
SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly


async def sweep_once() -> tuple[int, int, int, int]:
    """Run one maintenance pass.

    Returns (blobs_removed, sessions_removed, calls_closed, orphans_removed).
    """
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

        # ── Orphaned uploaded blobs ──────────────────────────────────────
        # Uploaded long ago but referenced by no message and no user avatar
        # (e.g. replaced profile pictures, or uploads whose send was never
        # completed). Referenced-id sets are collected in Python because
        # profile_picture_key stores the blob id as TEXT and cross-type SQL
        # comparison differs between Postgres (prod) and SQLite (tests).
        orphan_cutoff = now - UNREFERENCED_BLOB_MAX_AGE
        candidates = (await db.execute(
            select(MediaBlob).where(
                MediaBlob.uploaded_at.is_not(None),
                MediaBlob.created_at < orphan_cutoff,
            )
        )).scalars().all()
        orphans_removed = 0
        if candidates:
            referenced = {
                row for row in (await db.execute(
                    select(MessageMetadata.media_blob_id)
                    .where(MessageMetadata.media_blob_id.is_not(None))
                    .distinct()
                )).scalars().all()
            }
            avatar_keys = {
                row for row in (await db.execute(
                    select(User.profile_picture_key)
                    .where(User.profile_picture_key.is_not(None))
                )).scalars().all()
            }
            for blob in candidates:
                if blob.id in referenced or str(blob.id) in avatar_keys:
                    continue
                await storage.delete_bytes(blob.id)
                await db.delete(blob)
                orphans_removed += 1

        # ── Expired sessions ─────────────────────────────────────────────
        expired = await db.execute(
            delete(Session).where(Session.expires_at < now)
        )

        # ── Stale calls ──────────────────────────────────────────────────
        call_cutoff = now - STALE_CALL_MAX_AGE
        # Never picked up: close as missed at the time it started ringing.
        missed = await db.execute(
            update(Call)
            .where(
                Call.ended_at.is_(None),
                Call.accepted_at.is_(None),
                Call.started_at < call_cutoff,
            )
            .values(ended_at=Call.started_at, end_reason="missed")
        )
        # Connected but never hung up cleanly: close as completed now.
        completed = await db.execute(
            update(Call)
            .where(
                Call.ended_at.is_(None),
                Call.accepted_at.is_not(None),
                Call.started_at < call_cutoff,
            )
            .values(ended_at=now, end_reason="completed")
        )

        await db.commit()

    calls_closed = (missed.rowcount or 0) + (completed.rowcount or 0)
    return len(abandoned), expired.rowcount or 0, calls_closed, orphans_removed


async def run_maintenance_loop(
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep forever until the task is cancelled at shutdown."""
    while True:
        try:
            blobs, sessions, calls, orphans = await sweep_once()
            if blobs or sessions or calls or orphans:
                logger.info(
                    "Maintenance sweep: removed %d abandoned blob(s), "
                    "%d expired session(s), closed %d stale call(s), "
                    "removed %d orphaned blob(s)",
                    blobs, sessions, calls, orphans,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a bad sweep kill the loop — try again next interval.
            logger.exception("Maintenance sweep failed")
        await asyncio.sleep(interval_seconds)
