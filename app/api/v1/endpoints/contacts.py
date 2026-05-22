from __future__ import annotations
"""
app/api/v1/endpoints/contacts.py
─────────────────────────────────
Contact discovery — find another user by their 10-digit private_number.

POST /contacts/lookup
  Body: { "private_number": "0123456789" }
  Returns the public UserPublic view if found, or { "found": false }.

The Phase B / production version of this endpoint will accept SHA-256 hashes
of phone numbers (per the existing schema design). For Phase A — phoneless
identity — we look up by the user's chosen private_number directly.
"""

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.user import User
from app.schemas.messaging import (
    ContactLookupRequest,
    ContactLookupResponse,
    UserPublic,
)

router = APIRouter(prefix="/contacts", tags=["Contacts"])


@router.post(
    "/lookup",
    response_model=ContactLookupResponse,
    status_code=status.HTTP_200_OK,
    summary="Look up a user by 10-digit private number",
)
@limiter.limit("60/minute")
async def lookup_contact(
    request: Request,
    body: ContactLookupRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ContactLookupResponse:
    """
    Returns `{found: true, user: {...}}` when a user with this private_number
    exists and is active, otherwise `{found: false}`. Looking up your own
    number is allowed but rarely useful (the client should prevent it earlier).
    """
    result = await db.execute(
        select(User).where(
            User.private_number == body.private_number,
            User.is_active == True,  # noqa: E712  (SQLAlchemy needs ==)
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        return ContactLookupResponse(found=False, user=None)
    return ContactLookupResponse(
        found=True,
        user=UserPublic.model_validate(user),
    )
