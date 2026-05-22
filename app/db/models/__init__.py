from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/__init__.py
──────────────────────────
Import all models here so that Alembic's env.py can discover them
when running `alembic revision --autogenerate`.
"""

from app.db.models.call import Call
from app.db.models.contact import Contact
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.conversation_pref import ConversationPref
from app.db.models.deleted_message import DeletedMessage
from app.db.models.device import Device
from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from app.db.models.message_reaction import MessageReaction
from app.db.models.session import Session
from app.db.models.starred_message import StarredMessage
from app.db.models.user import User

__all__ = [
    "User",
    "Device",
    "Session",
    "Contact",
    "Conversation",
    "conversation_participants",
    "ConversationPref",
    "MediaBlob",
    "MessageMetadata",
    "MessageReaction",
    "StarredMessage",
    "DeletedMessage",
    "Call",
]
