from __future__ import annotations
"""
app/core/config.py
──────────────────
Application configuration loaded from environment variables via Pydantic Settings.
All settings are read once at startup from .env (or real env vars in production).
"""

from functools import lru_cache
from typing import Any, Union, Optional, Literal

from pydantic import AnyUrl, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


import os

_env_state = os.getenv("APP_ENV", "development")
# Default to .env.local in development, otherwise .env.production -> .env.production etc.
_env_file = ".env.local" if _env_state == "development" else f".env.{_env_state}"
# Optionally Fallback to .env exactly if the specified env file is somehow ignored, but pydantic ignores missing ones natively.

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_NAME: str = "Cricchat"
    # None = derive from APP_ENV (development → True). An explicit DEBUG env
    # var still wins — but a deployed box that forgets to set it must never
    # default to SQL echo + debug behaviour.
    DEBUG: Optional[bool] = None
    # Master switch for the slowapi rate limiter. Keep True in production; set
    # False only to run authenticated load/stress tests from a single IP (which
    # the per-IP limits would otherwise throttle). Re-enable immediately after.
    RATE_LIMITING_ENABLED: bool = True
    # Shown on the public /delete-account page as the fallback channel for
    # users who can no longer sign in. Google Play requires a reachable
    # contact there, so this must be a monitored inbox in production.
    SUPPORT_EMAIL: str = "support@cricchat.app"

    # ── Database ─────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/private_messenger"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalize_db_scheme(cls, v: str) -> str:
        # Railway (and Heroku) supply postgresql:// or postgres://, but
        # create_async_engine requires the postgresql+asyncpg:// dialect.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    # ── Redis ────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── JWT ──────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "changeme"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # Delete-intent tokens are issued by POST /login when the user supplies
    # their delete_password. They can only be used to call POST /confirm-delete
    # and nothing else. Short-lived so a leaked token has minimal blast radius.
    DELETE_INTENT_TOKEN_EXPIRE_MINUTES: int = 5

    # ── AWS (S3 media backend — unused until S3Storage is implemented) ───
    AWS_REGION: str = "ap-southeast-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    # ── AWS S3 ───────────────────────────────────────────────────────────
    AWS_S3_BUCKET_NAME: str = "private-messenger-media"
    AWS_S3_PRESIGN_EXPIRY_SECONDS: int = 3600

    # ── Media storage (Phase D) ──────────────────────────────────────────
    # `local` writes encrypted blobs to MEDIA_LOCAL_PATH and serves them via
    # the FastAPI app — works out of the box for dev. `s3` uses the AWS_S3_*
    # settings above (requires boto3 + valid credentials).
    MEDIA_STORAGE_BACKEND: Literal["local", "s3"] = "local"
    MEDIA_LOCAL_PATH: str = "./uploads"
    # Hard cap to keep disk + memory usage sane. 50 MB covers short videos.
    MEDIA_MAX_BLOB_BYTES: int = 50 * 1024 * 1024
    # How long a freshly-issued upload URL stays valid (server clock).
    MEDIA_UPLOAD_URL_TTL_SECONDS: int = 600

    # ── WebSocket protection ─────────────────────────────────────────────
    # Largest inbound WS frame we accept. Text messages are small; media
    # travels through the blob endpoints, so 64 KB is generous. Without a
    # cap a client can push frames up to uvicorn's 16 MB default straight
    # into Postgres and the Redis fan-out.
    WS_MAX_FRAME_BYTES: int = 64 * 1024
    # Per-connection inbound budget (events per 10s window). The user
    # channel gets a higher budget because ICE candidates arrive in bursts
    # during call setup.
    WS_CONV_EVENTS_PER_10S: int = 40
    WS_USER_EVENTS_PER_10S: int = 150

    # ── WebRTC (Phase E — calls) ─────────────────────────────────────────
    # Comma-separated STUN URIs. Public Google STUN works for most NAT
    # traversal in dev — production should add an authoritative STUN +
    # TURN under your control. Format: `stun:host:port,stun:other:port`.
    WEBRTC_STUN_URLS: str = "stun:stun.l.google.com:19302"
    # TURN is required when peers can't reach each other directly (≈20% of
    # mobile networks). Leave blank in dev — calls will work on most
    # networks with STUN alone but fail behind symmetric NATs. Drop in a
    # `coturn` deployment for production. Format: `turn:host:port`.
    WEBRTC_TURN_URL: str = ""
    WEBRTC_TURN_USERNAME: str = ""
    WEBRTC_TURN_PASSWORD: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8081"

    @computed_field  # type: ignore[misc]
    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @computed_field  # type: ignore[misc]
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @computed_field  # type: ignore[misc]
    @property
    def expose_docs(self) -> bool:
        # Interactive API docs (/docs, /redoc, /openapi.json) are a useful dev
        # affordance but leak the full API surface. Only serve them in local
        # development; never on a deployed (staging/production) box.
        return self.APP_ENV == "development"

    @model_validator(mode="after")
    def _default_debug_from_env(self) -> "Settings":
        if self.DEBUG is None:
            self.DEBUG = self.APP_ENV == "development"
        return self

    @model_validator(mode="after")
    def _require_real_secrets(self) -> "Settings":
        # The "changeme" defaults exist only so the app boots in local dev.
        # On any deployed environment, a forgotten env var would otherwise
        # silently fall back to a publicly-known secret — anyone could forge
        # JWTs for any user. Fail fast at startup instead.
        if self.APP_ENV != "development" and self.JWT_SECRET_KEY == "changeme":
            raise ValueError(
                f"Refusing to start in APP_ENV={self.APP_ENV!r}: "
                "JWT_SECRET_KEY still set to the insecure default "
                "'changeme'. Set a strong random value "
                "(e.g. `openssl rand -hex 32`)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (reads .env once)."""
    return Settings()


settings = get_settings()
