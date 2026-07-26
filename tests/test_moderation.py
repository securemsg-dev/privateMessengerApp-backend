from __future__ import annotations
"""
tests/test_moderation.py
─────────────────────────
Tests for the safety surface required by Google Play's UGC policy:

  - Blocking      (/api/v1/blocks)
  - Reporting     (/api/v1/reports)
  - Enforcement   (blocked messages are never persisted; blocked calls refused)
  - Discretion    (a block is never revealed to the blocked user)

The enforcement tests matter most: the whole design rests on a blocked message
being dropped at SEND time, so it can never resurface through history sync.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select
from starlette.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.message import MessageMetadata
from app.db.models.user import User
from app.db.models.user_report import ReportStatus, UserReport
from app.db.session import get_session
from app.main import create_app
from tests.conftest import (
    TestSessionLocal,
    auth_header,
    register_and_login,
)


# ── Blocking (REST) ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_block_user_and_list(client):
    """Blocking a user puts them on the caller's blocked list."""
    me = await register_and_login(client)
    them = await register_and_login(client)

    resp = await client.post(
        "/api/v1/blocks",
        json={"user_id": them["user"]["id"]},
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text

    listing = await client.get(
        "/api/v1/blocks",
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert listing.status_code == 200
    blocked = listing.json()["blocked"]
    assert len(blocked) == 1
    assert blocked[0]["user"]["id"] == them["user"]["id"]
    assert blocked[0]["blocked_at"]


@pytest.mark.asyncio
async def test_block_is_idempotent(client):
    """Blocking twice succeeds and does not create a duplicate row."""
    me = await register_and_login(client)
    them = await register_and_login(client)
    hdr = auth_header(me["tokens"]["access_token"])

    for _ in range(2):
        resp = await client.post(
            "/api/v1/blocks", json={"user_id": them["user"]["id"]}, headers=hdr,
        )
        assert resp.status_code == 201

    listing = await client.get("/api/v1/blocks", headers=hdr)
    assert len(listing.json()["blocked"]) == 1


@pytest.mark.asyncio
async def test_cannot_block_self(client):
    me = await register_and_login(client)
    resp = await client.post(
        "/api/v1/blocks",
        json={"user_id": me["user"]["id"]},
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_block_unknown_user_404(client):
    me = await register_and_login(client)
    resp = await client.post(
        "/api/v1/blocks",
        json={"user_id": str(uuid.uuid4())},
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unblock_removes_and_is_idempotent(client):
    me = await register_and_login(client)
    them = await register_and_login(client)
    hdr = auth_header(me["tokens"]["access_token"])
    them_id = them["user"]["id"]

    await client.post("/api/v1/blocks", json={"user_id": them_id}, headers=hdr)

    # First unblock removes it; second is a no-op rather than a 404.
    for _ in range(2):
        resp = await client.delete(f"/api/v1/blocks/{them_id}", headers=hdr)
        assert resp.status_code == 200

    listing = await client.get("/api/v1/blocks", headers=hdr)
    assert listing.json()["blocked"] == []


@pytest.mark.asyncio
async def test_block_status_only_reports_own_direction(client):
    """
    The status endpoint must never let you probe whether someone blocked YOU —
    it only ever answers about the block you own.
    """
    me = await register_and_login(client)
    them = await register_and_login(client)

    # THEY block ME.
    await client.post(
        "/api/v1/blocks",
        json={"user_id": me["user"]["id"]},
        headers=auth_header(them["tokens"]["access_token"]),
    )

    # I must not be able to see it.
    resp = await client.get(
        f"/api/v1/blocks/{them['user']['id']}",
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["blocked"] is False

    # And my blocked list stays empty.
    listing = await client.get(
        "/api/v1/blocks", headers=auth_header(me["tokens"]["access_token"]),
    )
    assert listing.json()["blocked"] == []


# ── Reporting (REST) ──────────────────────────────────────────────────────────

async def _report_by(db_session, reporter: dict) -> UserReport:
    """
    Fetch the single report filed by `reporter`.

    Filtered by reporter rather than selecting the whole table: the test DB is
    shared for the session and the endpoint commits, so reports from earlier
    tests are still present.
    """
    return (await db_session.execute(
        select(UserReport).where(
            UserReport.reporter_private_number == reporter["private_number"],
        )
    )).scalars().one()

@pytest.mark.asyncio
async def test_report_user_without_evidence(client, db_session):
    me = await register_and_login(client)
    them = await register_and_login(client)

    resp = await client.post(
        "/api/v1/reports",
        json={
            "user_id": them["user"]["id"],
            "reason": "harassment",
            "details": "Kept messaging after I asked them to stop.",
        },
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "received"

    report = await _report_by(db_session, me)
    assert report.reason.value == "harassment"
    assert report.status == ReportStatus.pending
    assert report.evidence is None
    # Private numbers are denormalised so the record survives account deletion.
    assert report.reported_private_number == them["private_number"]
    assert report.reporter_private_number == me["private_number"]


@pytest.mark.asyncio
async def test_report_with_consented_evidence_is_stored(client, db_session):
    """With include_messages=True the plaintext is persisted for moderators."""
    me = await register_and_login(client)
    them = await register_and_login(client)

    resp = await client.post(
        "/api/v1/reports",
        json={
            "user_id": them["user"]["id"],
            "reason": "hate_speech",
            "include_messages": True,
            "messages": [
                {"content": "first abusive message"},
                {"content": "second abusive message"},
            ],
        },
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text

    report = await _report_by(db_session, me)
    evidence = json.loads(report.evidence)
    assert [e["content"] for e in evidence] == [
        "first abusive message",
        "second abusive message",
    ]


@pytest.mark.asyncio
async def test_report_without_consent_discards_messages(client, db_session):
    """
    A client that sends message content WITHOUT setting include_messages must
    not get that plaintext stored. The consent flag is the gate, not the
    presence of a payload.
    """
    me = await register_and_login(client)
    them = await register_and_login(client)

    resp = await client.post(
        "/api/v1/reports",
        json={
            "user_id": them["user"]["id"],
            "reason": "spam",
            "include_messages": False,
            "messages": [{"content": "should never be stored"}],
        },
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 201

    report = await _report_by(db_session, me)
    assert report.evidence is None


@pytest.mark.asyncio
async def test_report_evidence_is_capped(client):
    """More than 20 attached messages is rejected, so reports can't be used
    as a backdoor bulk plaintext upload."""
    me = await register_and_login(client)
    them = await register_and_login(client)

    resp = await client.post(
        "/api/v1/reports",
        json={
            "user_id": them["user"]["id"],
            "reason": "spam",
            "include_messages": True,
            "messages": [{"content": f"msg {i}"} for i in range(21)],
        },
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cannot_report_self(client):
    me = await register_and_login(client)
    resp = await client.post(
        "/api/v1/reports",
        json={"user_id": me["user"]["id"], "reason": "spam"},
        headers=auth_header(me["tokens"]["access_token"]),
    )
    assert resp.status_code == 400


# ── Enforcement: WebSocket message send ───────────────────────────────────────

def _make_user(private_number: str) -> User:
    dummy = hash_password("Placeholder123")
    return User(
        private_number=private_number,
        login_password_hash=dummy,
        delete_password_hash=dummy,
    )


async def _setup_pair(sender_number: str, peer_number: str, block: bool):
    """
    Commit two users + a 1-to-1 conversation, optionally with the peer having
    blocked the sender. Returns (sender_id, peer_id, conversation_id).
    """
    async with TestSessionLocal() as session:
        sender = _make_user(sender_number)
        peer = _make_user(peer_number)
        session.add_all([sender, peer])
        await session.flush()

        conv = Conversation(is_group=False)
        session.add(conv)
        await session.flush()
        for uid in (sender.id, peer.id):
            await session.execute(
                insert(conversation_participants).values(
                    conversation_id=conv.id, user_id=uid,
                )
            )
        if block:
            session.add(BlockedUser(blocker_id=peer.id, blocked_id=sender.id))
        await session.commit()
        return sender.id, peer.id, conv.id


@pytest.fixture
def ws_client():
    """TestClient with the WS routers pointed at the test DB (mirrors
    tests/test_websocket.py::ws_app)."""
    import asyncio

    import fakeredis

    from app.core.limiter import limiter
    limiter.enabled = False

    app = create_app()

    async def _override_get_session():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session

    with patch(
        "app.main.aioredis.from_url",
        side_effect=lambda *a, **k: fakeredis.FakeAsyncRedis(decode_responses=False),
    ), patch(
        "app.main.run_maintenance_loop",
        new=lambda: asyncio.sleep(3600),
    ), patch(
        "app.websocket.router.AsyncSessionLocal", TestSessionLocal,
    ), patch(
        "app.websocket.user_router.AsyncSessionLocal", TestSessionLocal,
    ):
        with TestClient(app) as client:
            yield client


@pytest.mark.asyncio
async def test_blocked_message_is_never_persisted(ws_client):
    """
    The core guarantee: when the only other participant has blocked the
    sender, the message is dropped before the INSERT — so it cannot reappear
    later via history pagination or an unblock.
    """
    sender_id, _peer_id, conv_id = await _setup_pair(
        "2000000001", "2000000002", block=True,
    )
    token = create_access_token(sender_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text(json.dumps({
            "type": "message",
            "encrypted_payload": "ciphertext-blocked",
            "message_type": "text",
            "client_temp_id": "tmp-1",
        }))
        # The sender still gets their echo — they must NOT be able to tell.
        echo = json.loads(ws.receive_text())
        assert echo["type"] == "message"
        assert echo["client_temp_id"] == "tmp-1"
        assert echo["encrypted_payload"] == "ciphertext-blocked"

    async with TestSessionLocal() as session:
        rows = (await session.execute(
            select(MessageMetadata).where(
                MessageMetadata.conversation_id == conv_id,
            )
        )).scalars().all()
    assert rows == [], "blocked message must never be written to the DB"


@pytest.mark.asyncio
async def test_unblocked_message_is_persisted(ws_client):
    """Control case: the same flow without a block stores the message."""
    sender_id, _peer_id, conv_id = await _setup_pair(
        "2000000003", "2000000004", block=False,
    )
    token = create_access_token(sender_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text(json.dumps({
            "type": "message",
            "encrypted_payload": "ciphertext-ok",
            "message_type": "text",
            "client_temp_id": "tmp-2",
        }))
        echo = json.loads(ws.receive_text())
        assert echo["client_temp_id"] == "tmp-2"

    async with TestSessionLocal() as session:
        rows = (await session.execute(
            select(MessageMetadata).where(
                MessageMetadata.conversation_id == conv_id,
            )
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].encrypted_payload == "ciphertext-ok"


@pytest.mark.asyncio
async def test_echo_never_leaks_skip_hint(ws_client):
    """
    `_skip_user_ids` is a server-internal routing hint. If it ever reached a
    client it would tell the sender exactly who blocked them.
    """
    sender_id, _peer_id, conv_id = await _setup_pair(
        "2000000005", "2000000006", block=True,
    )
    token = create_access_token(sender_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text(json.dumps({
            "type": "message",
            "encrypted_payload": "ciphertext",
            "message_type": "text",
        }))
        echo = json.loads(ws.receive_text())
    assert "_skip_user_ids" not in echo


# ── Enforcement: calls ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_refused_when_blocked(client, db_session):
    """
    A blocked pair cannot open a call log row — otherwise both parties get a
    phantom "calling…" entry for a call that can never connect.
    """
    me = await register_and_login(client)
    them = await register_and_login(client)
    hdr = auth_header(me["tokens"]["access_token"])

    conv = await client.post(
        "/api/v1/conversations",
        json={"other_private_number": them["private_number"]},
        headers=hdr,
    )
    assert conv.status_code in (200, 201), conv.text
    conv_id = conv.json()["id"]

    # THEY block ME — the direction that must still stop me ringing them.
    await client.post(
        "/api/v1/blocks",
        json={"user_id": me["user"]["id"]},
        headers=auth_header(them["tokens"]["access_token"]),
    )

    resp = await client.post(
        "/api/v1/calls",
        json={"conversation_id": conv_id, "callee_id": them["user"]["id"]},
        headers=hdr,
    )
    assert resp.status_code == 403
    # The wording must not confirm a block exists.
    assert "block" not in resp.json()["detail"].lower()
