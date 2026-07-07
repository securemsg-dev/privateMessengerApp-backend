from __future__ import annotations
from typing import Literal, Optional
"""
app/schemas/auth.py
────────────────────
Pydantic request and response schemas for authentication endpoints.
Identity = 10-digit private_number. Two passwords: login + delete.
"""

import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Constants / validators ────────────────────────────────────────────────────

PRIVATE_NUMBER_PATTERN = re.compile(r"^\d{10}$")
MIN_PASSWORD_LEN = 8
# bcrypt hard-fails (ValueError) on inputs over 72 BYTES — without this guard
# a long passphrase turns into an HTTP 500 at registration. Measured in bytes,
# not chars, because multi-byte characters (emoji, CJK) count several times.
# Login deliberately has no max: verify_password() catches the ValueError and
# returns False, so existing credentials can never be locked out by a schema.
MAX_PASSWORD_BYTES = 72


def validate_private_number(v: str) -> str:
    if not PRIVATE_NUMBER_PATTERN.match(v):
        raise ValueError("private_number must be exactly 10 digits")
    return v


def validate_password_bytes(v: str) -> str:
    if len(v.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return v


# ── Request schemas ───────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=100)
    login_password: str = Field(min_length=MIN_PASSWORD_LEN)
    delete_password: str = Field(min_length=MIN_PASSWORD_LEN)

    @field_validator("login_password", "delete_password")
    @classmethod
    def _password_byte_limit(cls, v: str) -> str:
        return validate_password_bytes(v)

    @model_validator(mode="after")
    def passwords_must_differ(self) -> "RegisterRequest":
        if self.login_password == self.delete_password:
            raise ValueError("login_password and delete_password must be different")
        return self


class LoginRequest(BaseModel):
    """
    Login request. `login_password` accepts EITHER the user's login password
    (→ normal authentication) OR their delete password (→ delete-intent flow).
    The field name is kept for wire-compatibility; see LoginResponse /
    DeleteIntentResponse for the two possible outcomes.
    """
    private_number: str
    login_password: str

    @field_validator("private_number")
    @classmethod
    def _validate_private_number(cls, v: str) -> str:
        return validate_private_number(v)


class ConfirmDeleteRequest(BaseModel):
    delete_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Response schemas ──────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: UUID
    private_number: str
    display_name: Optional[str]
    is_active: bool

    model_config = {"from_attributes": True}


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RegisterResponse(BaseModel):
    user: UserResponse
    tokens: TokenPair
    private_number: str  # echoed for convenience on reveal screen


class LoginResponse(BaseModel):
    action: Literal["authenticated"] = "authenticated"
    user: UserResponse
    tokens: TokenPair


class DeleteIntentResponse(BaseModel):
    """
    Returned from POST /login when the supplied password matches the user's
    delete_password. Contains a short-lived delete-intent JWT the client must
    send back to POST /confirm-delete (after showing a warning dialog) to
    actually hard-delete the account.
    """
    action: Literal["confirm_delete"] = "confirm_delete"
    delete_token: str
    expires_in: int  # seconds until delete_token expires


class MessageResponse(BaseModel):
    message: str
