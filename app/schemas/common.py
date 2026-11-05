from __future__ import annotations
from typing import Optional, Union, Any
"""
app/schemas/common.py
──────────────────────
Generic response schemas shared across multiple endpoints.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    # Capabilities this backend supports. New app builds read it at startup
    # and hide features the server can't serve (e.g. after a rollback).
    features: list[str] = []


class ErrorResponse(BaseModel):
    detail: str
