from __future__ import annotations
"""
app/api/v1/endpoints/webrtc.py
───────────────────────────────
Phase E — runtime WebRTC config.

  GET /webrtc/config — returns ICE servers (STUN + optional TURN) the
                       client passes straight into `new RTCPeerConnection`.

Centralising this means rotating TURN credentials or moving servers does
NOT require an app update — clients refetch on every call setup.
"""

from fastapi import APIRouter, Request, status

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.core.limiter import limiter
from app.schemas.messaging import IceServer, WebRTCConfigResponse

router = APIRouter(prefix="/webrtc", tags=["WebRTC"])


def _build_ice_servers() -> list[IceServer]:
    """Compose the ice_servers list from current settings."""
    servers: list[IceServer] = []

    stun_urls = [
        u.strip() for u in settings.WEBRTC_STUN_URLS.split(",") if u.strip()
    ]
    if stun_urls:
        servers.append(IceServer(urls=stun_urls))

    if settings.WEBRTC_TURN_URL:
        servers.append(
            IceServer(
                urls=[settings.WEBRTC_TURN_URL],
                username=settings.WEBRTC_TURN_USERNAME or None,
                credential=settings.WEBRTC_TURN_PASSWORD or None,
            )
        )

    return servers


@router.get(
    "/config",
    response_model=WebRTCConfigResponse,
    status_code=status.HTTP_200_OK,
    summary="ICE server configuration for WebRTC peer connections",
)
@limiter.limit("60/minute")
async def get_webrtc_config(
    request: Request,
    current_user: CurrentUser,
) -> WebRTCConfigResponse:
    _ = current_user  # auth-gated; actual config is identical for all users
    return WebRTCConfigResponse(ice_servers=_build_ice_servers())
