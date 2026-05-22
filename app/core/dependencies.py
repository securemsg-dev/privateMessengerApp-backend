from __future__ import annotations
"""
app/core/dependencies.py
─────────────────────────
Shared FastAPI dependency functions injected via Depends().

Usage example:
    @router.get("/me")
    async def get_me(current_user: User = Depends(get_current_user)):
        ...
"""

from typing import Any, Union, Optional, Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_access_token
from app.db.models.user import User
from app.db.session import get_session

bearer_scheme = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """
    Validate the Bearer JWT and return the authenticated User ORM object.

    Raises HTTP 401 if token is missing, invalid, or expired.
    Raises HTTP 403 if the user account is deactivated.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = verify_access_token(credentials.credentials)
        user_id: str = payload.get("sub", "")
        if not user_id:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    from sqlalchemy import select

    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )
    return user


# ── Convenience type aliases ──────────────────────────────────────────────────

CurrentUser = Annotated[User, Depends(get_current_user)]
DBSession = Annotated[AsyncSession, Depends(get_session)]
