from __future__ import annotations
"""
app/schemas/messaging.py
─────────────────────────
Pydantic schemas for the messaging surface:
  - Contact lookup (find a user by 10-digit private number)
  - Conversation create / list (1-to-1 in Phase 1)
  - Message history (paginated by created_at DESC)
  - Profile (/users/me)
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.auth import validate_private_number


# ── User / profile ─────────────────────────────────────────────────────────────

class UserPublic(BaseModel):
    """Minimal public view of another user. Never includes password hashes.

    `public_key` is the user's long-term Curve25519 public key (Base64), used
    by clients to E2EE-encrypt messages. May be null until the client uploads
    it on first launch — clients should treat null as "encryption disabled".
    """
    id: UUID
    private_number: str
    display_name: Optional[str]
    public_key: Optional[str]
    profile_picture_key: Optional[str] = None

    model_config = {"from_attributes": True}


class ProfileUpdateRequest(BaseModel):
    """Partial update body for PATCH /users/me. None = leave field unchanged."""
    display_name: Optional[str] = Field(None, max_length=100)
    profile_picture_key: Optional[str] = Field(None, max_length=512)


# ── Contact lookup ─────────────────────────────────────────────────────────────

class ContactLookupRequest(BaseModel):
    private_number: str

    @field_validator("private_number")
    @classmethod
    def _validate(cls, v: str) -> str:
        return validate_private_number(v)


# ── Public key (E2EE — Phase B) ────────────────────────────────────────────────

class PublicKeyUploadRequest(BaseModel):
    """
    Upload (or replace) the caller's long-term Curve25519 public key.
    The key must be exactly 32 bytes when Base64-decoded.
    """
    public_key: str = Field(min_length=44, max_length=44)

    @field_validator("public_key")
    @classmethod
    def _validate_b64_32_bytes(cls, v: str) -> str:
        import base64
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("public_key must be valid base64") from exc
        if len(raw) != 32:
            raise ValueError("public_key must decode to exactly 32 bytes")
        return v


class ContactLookupResponse(BaseModel):
    found: bool
    user: Optional[UserPublic] = None


# ── Conversations ──────────────────────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    """
    Create-or-get a 1-to-1 conversation between the caller and another user
    identified by their 10-digit private_number. Idempotent — if a conversation
    already exists between these two users, it is returned unchanged.
    """
    other_private_number: str

    @field_validator("other_private_number")
    @classmethod
    def _validate(cls, v: str) -> str:
        return validate_private_number(v)


class LastMessagePreview(BaseModel):
    id: UUID
    sender_id: UUID
    message_type: Literal["text", "voice", "image"]
    encrypted_payload: str
    created_at: datetime
    self_destruct: bool

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    """
    A conversation as seen by the current user. `other_participant` is the
    OTHER user in a 1:1 chat (server resolves this so the client doesn't have
    to introspect the participants list). For groups (Phase 2+) this would be
    null and the `participants` array would carry everyone.

    Per-user prefs (pin / mute / mark unread) are inlined here — defaults are
    used when the user has never touched the conversation's prefs.
    """
    id: UUID
    is_group: bool
    name: Optional[str]
    created_at: datetime
    other_participant: Optional[UserPublic]
    last_message: Optional[LastMessagePreview]
    unread_count: int = 0

    # ── Per-user prefs (Phase C.1) ────────────────────────────────────
    is_pinned: bool = False
    mute_until: Optional[datetime] = None
    manual_unread: bool = False

    model_config = {"from_attributes": True}


class ConversationPrefsUpdate(BaseModel):
    """
    Partial-update body for PATCH /conversations/{id}/prefs.

    Only fields explicitly present in the request are applied — the endpoint
    uses `model_fields_set` to distinguish "field omitted" from "field set to
    a default value". `mute_seconds`:
        > 0  → mute for that many seconds (server computes mute_until)
        == 0 → unmute (clears mute_until)
    """
    is_pinned: Optional[bool] = None
    mute_seconds: Optional[int] = Field(None, ge=0)
    manual_unread: Optional[bool] = None


# ── Messages ───────────────────────────────────────────────────────────────────

class ReactionAggregate(BaseModel):
    """
    One emoji's tally on a single message — `count` is the total number of
    users who reacted with this emoji, and `by_me` is true when the *caller*
    is one of those users (drives the "tap again to remove" toggle UX).
    """
    emoji: str
    count: int
    by_me: bool


class MessageReplyPreview(BaseModel):
    """
    Tiny snapshot of the message being replied to, hydrated server-side so
    clients don't have to backfill if the original scrolled out of memory.
    `encrypted_payload` is still opaque to the server — client decrypts it
    using the existing peer key (Phase B).
    """
    id: UUID
    sender_id: UUID
    message_type: Literal["text", "voice", "image"]
    encrypted_payload: str

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    """
    A single message metadata row. The `encrypted_payload` is opaque to the
    server — it's whatever the client sent over WebSocket. Phase B will swap
    this from "plaintext for testing" to actual ciphertext without changing
    the wire shape.

    `reactions` aggregates emoji counts; `is_starred` is the *caller's*
    private bookmark; `reply_to_id` + `reply_preview` carry the inline-reply
    threading metadata (Phase C.2). `deleted_at` + `deleted_by` are set when
    the sender wiped the message for everyone (Phase C.3) — when this
    happens the server clears `encrypted_payload` to an empty string and
    clients render a tombstone in place.
    """
    id: UUID
    conversation_id: UUID
    sender_id: UUID
    message_type: Literal["text", "voice", "image"]
    encrypted_payload: str
    created_at: datetime
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]
    self_destruct: bool
    reactions: list[ReactionAggregate] = []
    is_starred: bool = False
    reply_to_id: Optional[UUID] = None
    reply_preview: Optional[MessageReplyPreview] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[UUID] = None

    model_config = {"from_attributes": True}


class StarMessageRequest(BaseModel):
    """Body for POST /messages/{id}/star — explicit set/clear, not a toggle."""
    starred: bool


# ── Media (Phase D) ────────────────────────────────────────────────────────────

class MediaUploadRequest(BaseModel):
    """
    Client tells the server what it's about to upload (size + mime). The
    server reserves a blob_id, returns an upload URL, and the client PUTs
    the encrypted bytes there. We never see plaintext.
    """
    size_bytes: int = Field(gt=0)
    mime: str = Field(min_length=1, max_length=128)


class MediaUploadResponse(BaseModel):
    """Issued upload coordinates. `download_url` is what the *recipient*
    hits later to fetch the ciphertext (after the sender finishes PUTting)."""
    blob_id: UUID
    upload_url: str
    download_url: str
    expires_at: datetime
    max_bytes: int


# ── WebRTC (Phase E) ──────────────────────────────────────────────────────────

class IceServer(BaseModel):
    """One entry in the RTCConfiguration.iceServers array — STUN or TURN."""
    urls: list[str]
    username: Optional[str] = None
    credential: Optional[str] = None


class WebRTCConfigResponse(BaseModel):
    """
    Returned from GET /webrtc/config — the client passes this straight to
    `new RTCPeerConnection({ iceServers })`. Includes any TURN credentials
    the operator has provisioned.
    """
    ice_servers: list[IceServer]


# ── Call history (Phase E) ────────────────────────────────────────────────────

CallEndReason = Literal["completed", "declined", "missed", "cancelled", "failed"]


class CallCreateRequest(BaseModel):
    """Caller logs the start of a call. Returns a call_id used in signaling."""
    conversation_id: UUID
    callee_id: UUID


class CallUpdateRequest(BaseModel):
    """
    Both caller and callee may PATCH a call they participated in. Pass any
    subset of fields. `accepted_at` is set ONCE by the callee on pick-up;
    `ended_at` + `end_reason` are set once on hang-up by either side.
    """
    accepted_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    end_reason: Optional[CallEndReason] = None


class CallResponse(BaseModel):
    id: UUID
    conversation_id: Optional[UUID]
    caller_id: Optional[UUID]
    callee_id: Optional[UUID]
    started_at: datetime
    accepted_at: Optional[datetime]
    ended_at: Optional[datetime]
    end_reason: Optional[CallEndReason]

    model_config = {"from_attributes": True}


class DeleteMessageRequest(BaseModel):
    """
    Body for POST /messages/{id}/delete.

    `scope`:
      - "me"       — caller-only hide; row added to `deleted_messages`.
      - "everyone" — wipes encrypted_payload for all participants. Allowed
                     only by the sender within DELETE_FOR_EVERYONE_WINDOW
                     (24h by default). Broadcasts a `deletion` WS event.
    """
    scope: Literal["me", "everyone"]


class MessagePage(BaseModel):
    """One page of message history. `next_cursor` is the `created_at` of the
    oldest item in this page; pass it back as `?before=...` to get the next
    older page. Null when there are no more messages."""
    messages: list[MessageResponse]
    next_cursor: Optional[datetime]
