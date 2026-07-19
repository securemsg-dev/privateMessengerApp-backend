from __future__ import annotations
from typing import Optional, Union, Any
"""
app/main.py
────────────
FastAPI application entry point.

Startup sequence:
  1. Connect to Redis (stored in app.state.redis)
  2. Start Redis pub/sub subscriber as a background task
  3. The subscriber delivers incoming Redis messages to local WebSocket connections

Shutdown sequence:
  1. Cancel the subscriber task
  2. Close the Redis connection
  3. Dispose SQLAlchemy engine pool

Access Swagger UI at: http://localhost:8000/docs
Access ReDoc at:      http://localhost:8000/redoc
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.endpoints import health, legal
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.limiter import limiter
from app.db.session import engine
from app.services.maintenance import run_maintenance_loop
from app.services.media_storage import get_storage
from app.services.push_service import close_http_client
from app.websocket.manager import manager
from app.websocket.router import router as ws_router
from app.websocket.user_router import router as user_ws_router

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup → yield → shutdown."""

    # ── Startup ───────────────────────────────────────────────────────────
    logger.info("Starting %s [%s]", settings.APP_NAME, settings.APP_ENV)

    # Fail fast if the configured media backend is unusable (e.g. the S3
    # stub) instead of erroring on the first upload in production.
    get_storage()

    # Connect to Redis. health_check_interval proactively re-validates
    # long-idle pooled connections instead of failing the first publish
    # after a quiet period.
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=False,
        health_check_interval=30,
    )
    app.state.redis = redis_client
    logger.info("Redis connected: %s", settings.REDIS_URL)

    # Start the Redis pub/sub subscriber task (one per process)
    subscriber_task = asyncio.create_task(
        manager.start_subscriber(redis_client),
        name="redis-pubsub-subscriber",
    )
    logger.info("Redis pub/sub subscriber started")

    # Hourly housekeeping: abandoned media blobs + expired sessions
    maintenance_task = asyncio.create_task(
        run_maintenance_loop(),
        name="maintenance-sweeper",
    )
    logger.info("Maintenance sweeper started")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("Shutting down %s", settings.APP_NAME)
    for task in (subscriber_task, maintenance_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await close_http_client()
    await redis_client.aclose()
    await engine.dispose()
    logger.info("Shutdown complete")


# ── Application Factory ───────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        description=(
            "Cricchat Backend API\n\n"
            "Provides authentication (private number + password), device "
            "registration, and real-time messaging via WebSocket with Redis "
            "pub/sub."
        ),
        version="1.0.0",
        # Interactive docs are served only in local development — on a deployed
        # box these are disabled so the full API surface isn't published.
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url="/redoc" if settings.expose_docs else None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
        lifespan=lifespan,
    )

    # ── Rate limiter ──────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── CORS ──────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routes ────────────────────────────────────────────────────────────
    app.include_router(health.router)  # /health at root
    # /privacy and /delete-account at root — both must be publicly reachable
    # in a browser for the Play Store listing and Data safety declarations.
    app.include_router(legal.router)
    app.include_router(api_router, prefix="/api/v1")
    # ORDER MATTERS: the static /ws/user route MUST be registered before the
    # parametrized /ws/{conversation_id} route. Starlette matches in
    # registration order, and "/ws/user" otherwise matches /ws/{conversation_id}
    # with conversation_id="user" — which fails UUID coercion and rejects the
    # connection with 403 before accept(). That collision silently broke ALL
    # call signaling (the per-user WS never connected).
    app.include_router(user_ws_router)  # WebSocket at /ws/user (Phase E call signaling)
    app.include_router(ws_router)       # WebSocket at /ws/{conversation_id}

    return app


app = create_app()
