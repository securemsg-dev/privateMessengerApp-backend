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


class ErrorResponse(BaseModel):
    detail: str
