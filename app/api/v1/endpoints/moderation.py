from __future__ import annotations
"""
app/api/v1/endpoints/moderation.py
───────────────────────────────────
Safety surface — required by Google Play's User Generated Content policy.

  POST   /blocks             — block a user
  DELETE /blocks/{user_id}   — unblock a user
  GET    /blocks             — list who the caller has blocked
  GET    /blocks/{user_id}   — has the caller blocked this user?
  POST   /reports            — report a user, optionally with message evidence

Two invariants run through this whole file:

  1. A blocked user is NEVER told they were blocked. No endpoint here reveals
     the reverse direction of a block, and blocked sends fail silently (see
     app/services/moderation_service.py).
  2. Plaintext message evidence is stored ONLY when the client passes
     include_messages=True, which mirrors an explicit consent dialog. Without
     that flag any `messages` payload is dropped on the floor.
"""

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import delete, select

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.blocked_user import BlockedUser
from app.db.models.user import User
from app.db.models.user_report import UserReport
from app.schemas.auth import MessageResponse
from app.schemas.messaging import UserPublic
from app.schemas.moderation import (
    BlockedUserEntry,
    BlockListResponse,
    BlockRequest,
    BlockStatusResponse,
    ReportRequest,
    ReportResponse,
)

router = APIRouter(tags=["Safety"])


async def _load_target(db, user_id: UUID) -> User:
    """Fetch the target user or 404. Used by both block and report."""
    user = (await db.execute(
        select(User).where(User.id == user_id, User.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


# ── Blocking ───────────────────────────────────────────────────────────────────

@router.post(
    "/blocks",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Block a user",
)
@limiter.limit("60/minute")
async def block_user(
    request: Request,
    body: BlockRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> MessageResponse:
    """
    Block `user_id`. Idempotent — blocking someone already blocked succeeds
    without creating a duplicate row.

    Effective immediately: their messages stop being persisted or delivered,
    and calls between the two of you are refused in both directions.
    """
    if body.user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot block yourself",
        )

    await _load_target(db, body.user_id)

    existing = (await db.execute(
        select(BlockedUser).where(
            BlockedUser.blocker_id == current_user.id,
            BlockedUser.blocked_id == body.user_id,
        )
    )).scalar_one_or_none()
    if existing is None:
        db.add(BlockedUser(blocker_id=current_user.id, blocked_id=body.user_id))
        await db.commit()

    return MessageResponse(message="User blocked.")


@router.delete(
    "/blocks/{user_id}",
    response_model=MessageResponse,
    summary="Unblock a user",
)
@limiter.limit("60/minute")
async def unblock_user(
    request: Request,
    user_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> MessageResponse:
    """
    Remove a block. Idempotent — unblocking someone who isn't blocked is a
    no-op rather than a 404, so the client never has to special-case it.

    Note this does not restore anything: messages sent while the block was
    active were never stored, so they do not reappear.
    """
    await db.execute(
        delete(BlockedUser).where(
            BlockedUser.blocker_id == current_user.id,
            BlockedUser.blocked_id == user_id,
        )
    )
    await db.commit()
    return MessageResponse(message="User unblocked.")


@router.get(
    "/blocks",
    response_model=BlockListResponse,
    summary="List users the caller has blocked",
)
@limiter.limit("60/minute")
async def list_blocks(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
) -> BlockListResponse:
    """Powers the Blocked Users screen in Settings."""
    rows = (await db.execute(
        select(BlockedUser, User)
        .join(User, User.id == BlockedUser.blocked_id)
        .where(BlockedUser.blocker_id == current_user.id)
        .order_by(BlockedUser.created_at.desc())
    )).all()

    return BlockListResponse(
        blocked=[
            BlockedUserEntry(
                user=UserPublic.model_validate(user),
                blocked_at=block.created_at,
            )
            for block, user in rows
        ]
    )


@router.get(
    "/blocks/{user_id}",
    response_model=BlockStatusResponse,
    summary="Check whether the caller has blocked a user",
)
@limiter.limit("120/minute")
async def block_status(
    request: Request,
    user_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> BlockStatusResponse:
    """
    Only ever reports the caller's own outgoing block, so this can't be used
    to probe whether someone else has blocked you.
    """
    row = (await db.execute(
        select(BlockedUser.id).where(
            BlockedUser.blocker_id == current_user.id,
            BlockedUser.blocked_id == user_id,
        )
    )).first()
    return BlockStatusResponse(blocked=row is not None)


# ── Reporting ──────────────────────────────────────────────────────────────────

@router.post(
    "/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Report a user for abuse",
)
@limiter.limit("10/hour")
async def report_user(
    request: Request,
    body: ReportRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ReportResponse:
    """
    File an abuse report. Rate-limited to 10/hour so reporting can't itself
    become a harassment vector.

    Message evidence is attached only when `include_messages` is True. The
    reported account is not notified, and the response deliberately reveals
    nothing about what action follows.
    """
    if body.user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot report yourself",
        )

    target = await _load_target(db, body.user_id)

    # Consent gate: without include_messages we never persist plaintext, even
    # if the client sent some.
    evidence_json: str | None = None
    if body.include_messages and body.messages:
        evidence_json = json.dumps(
            [
                {
                    "message_id": str(m.message_id) if m.message_id else None,
                    "sender_id": str(m.sender_id) if m.sender_id else None,
                    "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                    "content": m.content,
                }
                for m in body.messages
            ],
            ensure_ascii=False,
        )

    report = UserReport(
        reporter_id=current_user.id,
        reported_id=target.id,
        reporter_private_number=current_user.private_number,
        reported_private_number=target.private_number,
        conversation_id=body.conversation_id,
        reason=body.reason,
        details=body.details,
        evidence=evidence_json,
    )
    db.add(report)
    await db.commit()

    return ReportResponse(
        report_id=report.id,
        status="received",
        message=(
            "Thanks — your report has been received and will be reviewed "
            "within 24 hours."
        ),
    )
