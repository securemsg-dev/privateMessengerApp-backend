from __future__ import annotations
from typing import Optional, Union, Any
"""
app/api/v1/endpoints/devices.py
────────────────────────────────
Device management endpoints (authenticated):
  POST /devices/register   — Register or update a device + push token + public key
  GET  /devices            — List all devices registered to the current user
  POST /devices/clear-push — Detach a push token from the caller's devices (logout)
"""

from fastapi import APIRouter, Request, Response, status

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.schemas.device import (
    DeviceRegisterRequest,
    DeviceResponse,
    PushTokenClearRequest,
)
from app.services import device_service

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.post(
    "/register",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register or update a device",
)
@limiter.limit("10/minute")
async def register_device(
    request: Request,
    body: DeviceRegisterRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> DeviceResponse:
    """
    Register a device for the authenticated user.
    Also stores the push notification token (APNs/FCM) and E2EE public key.
    If a device with the same push_token already exists, it is updated.
    """
    device = await device_service.register_device(
        user_id=current_user.id,
        device_name=body.device_name,
        platform=body.platform,
        push_token=body.push_token,
        public_key=body.public_key,
        db=db,
    )
    return DeviceResponse.model_validate(device)


@router.get(
    "",
    response_model=list[DeviceResponse],
    summary="List registered devices",
)
@limiter.limit("60/minute")
async def list_devices(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
) -> list[DeviceResponse]:
    """
    Return all devices registered to the authenticated user.
    """
    devices = await device_service.list_user_devices(current_user.id, db)
    return [DeviceResponse.model_validate(d) for d in devices]


@router.post(
    "/clear-push",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Detach a push token from the caller's devices (call before logout)",
)
@limiter.limit("10/minute")
async def clear_push_token(
    request: Request,
    body: PushTokenClearRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    """
    Null out the push token on the caller's matching device rows so a phone
    that has signed out stops receiving this account's notifications.
    Idempotent — clearing an unknown token is a silent no-op.
    """
    await device_service.clear_push_token(current_user.id, body.push_token, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
