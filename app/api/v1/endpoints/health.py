from __future__ import annotations
from typing import Optional, Union, Any
"""
app/api/v1/endpoints/health.py
────────────────────────────────
Health check endpoint — used by load balancers and monitoring.
No authentication required.
"""

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.common import HealthResponse


def enabled_features() -> list[str]:
    """Capability flags advertised to clients (see HealthResponse.features)."""
    features = ["video"]
    if settings.MEDIA_STORAGE_BACKEND == "s3":
        features.append("direct_upload")
    return features

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    """
    Returns the service health status.
    Used by AWS ALB / Route53 health checks.
    """
    return HealthResponse(status="ok", version="1.0.0", features=enabled_features())
