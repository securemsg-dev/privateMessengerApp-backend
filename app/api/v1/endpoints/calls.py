from __future__ import annotations
"""
app/api/v1/endpoints/calls.py
──────────────────────────────
Phase E — call history.

  POST  /calls          — caller creates a row when initiating a call
  PATCH /calls/{id}     — either party records accept / end / reason
  GET   /calls          — current user's call list (caller OR callee)

WebRTC signaling itself does NOT go through these endpoints — it travels
over /ws/user. These routes are purely the persistent log shown in the
Calls tab.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import or_, select

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.call import Call
from app.db.models.conversation import conversation_participants
from app.db.models.user import User
from app.schemas.messaging import (
    CallCreateRequest,
    CallResponse,
    CallUpdateRequest,
)

router = APIRouter(prefix="/calls", tags=["Calls"])


@router.post(
    "",
    response_model=CallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log the start of a call (caller-initiated)",
)
@limiter.limit("30/minute")
async def create_call(
    request: Request,
    body: CallCreateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> CallResponse:
    if body.callee_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot call yourself",
        )

    # Both parties must be participants of the conversation
    rows = (
        await db.execute(
            select(conversation_participants.c.user_id).where(
                conversation_participants.c.conversation_id == body.conversation_id,
                conversation_participants.c.user_id.in_(
                    [current_user.id, body.callee_id]
                ),
            )
        )
    ).scalars().all()
    if set(rows) != {current_user.id, body.callee_id}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Both parties must be in the conversation",
        )

    call = Call(
        conversation_id=body.conversation_id,
        caller_id=current_user.id,
        callee_id=body.callee_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(call)
    await db.flush()
    return CallResponse.model_validate(call)


@router.patch(
    "/{call_id}",
    response_model=CallResponse,
    summary="Update accept / end / end_reason for an in-flight or finished call",
)
@limiter.limit("60/minute")
async def update_call(
    request: Request,
    call_id: UUID,
    body: CallUpdateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> CallResponse:
    call = (
        await db.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if current_user.id not in (call.caller_id, call.callee_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only call participants can update this call",
        )

    fields = body.model_fields_set
    if "accepted_at" in fields and call.accepted_at is None:
        # Only the callee can set accepted_at; caller picking up makes no sense
        if current_user.id == call.callee_id:
            call.accepted_at = body.accepted_at
    if "ended_at" in fields and call.ended_at is None:
        call.ended_at = body.ended_at
    if "end_reason" in fields and call.end_reason is None:
        call.end_reason = body.end_reason

    await db.flush()
    return CallResponse.model_validate(call)


@router.get(
    "",
    response_model=list[CallResponse],
    summary="List the current user's calls (caller OR callee), newest first",
)
@limiter.limit("60/minute")
async def list_calls(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
) -> list[CallResponse]:
    rows = (
        await db.execute(
            select(Call)
            .where(
                or_(
                    Call.caller_id == current_user.id,
                    Call.callee_id == current_user.id,
                )
            )
            .order_by(Call.started_at.desc())
            .limit(200)
        )
    ).scalars().all()

    # Resolve the "other" party of each call to a real name/number in one query
    # so the Calls tab never has to fall back to a raw UUID slice.
    peer_ids = {
        c.callee_id if c.caller_id == current_user.id else c.caller_id
        for c in rows
    }
    peer_ids.discard(None)
    peers: dict[UUID, User] = {}
    if peer_ids:
        peer_rows = (
            await db.execute(select(User).where(User.id.in_(peer_ids)))
        ).scalars().all()
        peers = {u.id: u for u in peer_rows}

    result: list[CallResponse] = []
    for c in rows:
        peer_id = c.callee_id if c.caller_id == current_user.id else c.caller_id
        peer = peers.get(peer_id) if peer_id else None
        dto = CallResponse.model_validate(c)
        if peer is not None:
            dto.peer_display_name = peer.display_name
            dto.peer_private_number = peer.private_number
        result.append(dto)
    return result
