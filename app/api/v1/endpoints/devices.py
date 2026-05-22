from __future__ import annotations
from typing import Optional, Union, Any
"""
app/api/v1/endpoints/devices.py
────────────────────────────────
Device management endpoints (authenticated):
  POST /devices/register — Register or update a device + push token + public key
  GET  /devices          — List all devices registered to the current user
"""

from fastapi import APIRouter, Request, status

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.schemas.device import DeviceRegisterRequest, DeviceResponse
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
