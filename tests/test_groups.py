"""
Group chat: membership rules, visibility, read state, safety, and the E2EE
member-set check on the WebSocket.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import insert, select

from app.core.security import create_access_token
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.message import MessageMetadata, MessageType
from app.db.models.user import User
from tests.conftest import (
    TestSessionLocal,
    auth_header,
    create_conversation_with_participants,
    register_and_login,
)
from tests.test_websocket import ws_app, ws_client  # noqa: F401 — fixtures

cp = conversation_participants


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


async def _ready_user(client, db_session, *, name: str, supports_groups: bool = True, key: bool = True):
    """Registered user with an E2EE key and a groups-capable app build."""
    reg = await register_and_login(client)
    uid = UUID(reg["user"]["id"])
    user = await db_session.get(User, uid)
    user.display_name = name
    user.public_key = _key() if key else None
    user.supports_groups = supports_groups
    await db_session.flush()
    return reg, uid


async def _befriend(db_session, a: UUID, b: UUID) -> None:
    await create_conversation_with_participants(db_session, [a, b])


def _h(reg) -> dict:
    return auth_header(reg["tokens"]["access_token"])


async def _create_group(client, owner_reg, member_ids, name="Family"):
    return await client.post(
        "/api/v1/conversations/groups",
        json={"name": name, "member_ids": [str(m) for m in member_ids]},
        headers=_h(owner_reg),
    )


@pytest.fixture
async def trio(client, db_session):
    """Alice (owner) with contacts Bob and Carol."""
    alice = await _ready_user(client, db_session, name="Alice")
    bob = await _ready_user(client, db_session, name="Bob")
    carol = await _ready_user(client, db_session, name="Carol")
    await _befriend(db_session, alice[1], bob[1])
    await _befriend(db_session, alice[1], carol[1])
    return alice, bob, carol


# ── Creation & visibility ─────────────────────────────────────────────────────

async def test_create_group(client, db_session, trio):
    (alice, alice_id), (bob, bob_id), (_, carol_id) = trio
    resp = await _create_group(client, alice, [bob_id, carol_id])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["is_group"] is True and data["name"] == "Family"
    assert data["my_role"] == "admin"
    roles = {m["id"]: m["role"] for m in data["participants"]}
    assert roles == {str(alice_id): "admin", str(bob_id): "member", str(carol_id): "member"}
    # Members never expose private numbers to each other.
    assert all("private_number" not in m for m in data["participants"])
    assert data["other_participant"] is None
    assert data["last_message"]["message_type"] == "system"
    event = json.loads(data["last_message"]["encrypted_payload"])
    assert event["event"] == "group_created" and event["actor_name"] == "Alice"


async def test_groups_hidden_from_old_builds(client, db_session, trio):
    (alice, _), (bob, bob_id), (_, carol_id) = trio
    await _create_group(client, alice, [bob_id, carol_id])

    old = await client.get("/api/v1/conversations", headers=_h(bob))
    assert all(not c["is_group"] for c in old.json())

    new = await client.get("/api/v1/conversations?include_groups=true", headers=_h(bob))
    groups = [c for c in new.json() if c["is_group"]]
    assert len(groups) == 1 and groups[0]["my_role"] == "member"


async def test_listing_with_groups_marks_user_group_capable(client, db_session):
    reg, uid = await _ready_user(client, db_session, name="Dana", supports_groups=False)
    await client.get("/api/v1/conversations?include_groups=true", headers=_h(reg))
    assert (await db_session.get(User, uid)).supports_groups is True


@pytest.mark.parametrize(
    "setup, status",
    [
        ("stranger", 403),
        ("no_key", 422),
        ("old_build", 409),
        ("blocked", 403),
    ],
)
async def test_add_rules(client, db_session, trio, setup, status):
    (alice, alice_id), (_, bob_id), _ = trio
    target, target_id = await _ready_user(
        client, db_session, name="Eve",
        supports_groups=setup != "old_build", key=setup != "no_key",
    )
    if setup != "stranger":
        await _befriend(db_session, alice_id, target_id)
    if setup == "blocked":
        db_session.add(BlockedUser(blocker_id=target_id, blocked_id=alice_id))
        await db_session.flush()
    resp = await _create_group(client, alice, [bob_id, target_id])
    assert resp.status_code == status, resp.text


async def test_member_limit(client, db_session, trio):
    (alice, _), (_, bob_id), (_, carol_id) = trio
    with patch("app.services.group_service.settings.GROUP_MAX_MEMBERS", 2):
        resp = await _create_group(client, alice, [bob_id, carol_id])
    assert resp.status_code == 422


# ── Membership changes ────────────────────────────────────────────────────────

async def test_admin_only_management(client, db_session, trio):
    (alice, alice_id), (bob, bob_id), (_, carol_id) = trio
    gid = (await _create_group(client, alice, [bob_id])).json()["id"]

    # Bob (member) can't add, remove, or rename.
    await _befriend(db_session, bob_id, carol_id)
    r = await client.post(f"/api/v1/conversations/{gid}/members", json={"member_ids": [str(carol_id)]}, headers=_h(bob))
    assert r.status_code == 403
    r = await client.delete(f"/api/v1/conversations/{gid}/members/{alice_id}", headers=_h(bob))
    assert r.status_code == 403
    r = await client.patch(f"/api/v1/conversations/{gid}", json={"name": "Hijacked"}, headers=_h(bob))
    assert r.status_code == 403

    # Alice (admin) can.
    r = await client.post(f"/api/v1/conversations/{gid}/members", json={"member_ids": [str(carol_id)]}, headers=_h(alice))
    assert r.status_code == 200 and len(r.json()["participants"]) == 3
    r = await client.patch(f"/api/v1/conversations/{gid}", json={"name": "  Weekend  plans "}, headers=_h(alice))
    assert r.status_code == 200 and r.json()["name"] == "Weekend plans"


async def test_new_member_sees_nothing_from_before_joining(client, db_session, trio):
    (alice, alice_id), (_, bob_id), (carol, carol_id) = trio
    gid = UUID((await _create_group(client, alice, [bob_id])).json()["id"])
    db_session.add(MessageMetadata(
        conversation_id=gid, sender_id=alice_id, message_type=MessageType.text,
        encrypted_payload="before", created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    await db_session.flush()

    await client.post(f"/api/v1/conversations/{gid}/members", json={"member_ids": [str(carol_id)]}, headers=_h(alice))
    page = (await client.get(f"/api/v1/conversations/{gid}/messages", headers=_h(carol))).json()
    payloads = [m["encrypted_payload"] for m in page["messages"]]
    assert "before" not in payloads
    assert [json.loads(p)["event"] for p in payloads] == ["members_added"]


async def test_removed_member_loses_access(client, db_session, trio):
    (alice, _), (bob, bob_id), (_, carol_id) = trio
    gid = (await _create_group(client, alice, [bob_id, carol_id])).json()["id"]
    r = await client.delete(f"/api/v1/conversations/{gid}/members/{bob_id}", headers=_h(alice))
    assert r.status_code == 204
    r = await client.get(f"/api/v1/conversations/{gid}/messages", headers=_h(bob))
    assert r.status_code == 403
    last = (await client.get(f"/api/v1/conversations/{gid}", headers=_h(alice))).json()["last_message"]
    assert json.loads(last["encrypted_payload"])["event"] == "member_removed"


async def test_admin_leaving_promotes_oldest_member_and_empty_group_is_deleted(client, db_session, trio):
    (alice, _), (bob, bob_id), (carol, carol_id) = trio
    gid = (await _create_group(client, alice, [bob_id, carol_id])).json()["id"]

    # Chat-list "delete" on a group = leave.
    assert (await client.delete(f"/api/v1/conversations/{gid}", headers=_h(alice))).status_code == 204
    data = (await client.get(f"/api/v1/conversations/{gid}", headers=_h(bob))).json()
    assert {m["role"] for m in data["participants"]} == {"admin", "member"}
    assert any(m["role"] == "admin" for m in data["participants"])

    await client.delete(f"/api/v1/conversations/{gid}/members/{bob_id}", headers=_h(bob))
    await client.delete(f"/api/v1/conversations/{gid}/members/{carol_id}", headers=_h(carol))
    assert await db_session.get(Conversation, UUID(gid)) is None


async def test_account_deletion_hands_over_admin(client, db_session, trio):
    from app.services.auth_service import delete_user_by_id

    (alice, alice_id), (bob, bob_id), (_, carol_id) = trio
    gid = UUID((await _create_group(client, alice, [bob_id, carol_id])).json()["id"])
    await delete_user_by_id(alice_id, db_session)
    await db_session.flush()
    roles = (await db_session.execute(select(cp.c.role).where(cp.c.conversation_id == gid))).scalars().all()
    assert sorted(roles) == ["admin", "member"]


# ── Read state & safety ───────────────────────────────────────────────────────

async def test_unread_is_per_member(client, db_session, trio):
    (alice, alice_id), (bob, bob_id), (carol, carol_id) = trio
    gid = UUID((await _create_group(client, alice, [bob_id, carol_id])).json()["id"])
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(MessageMetadata(
            conversation_id=gid, sender_id=alice_id, message_type=MessageType.text,
            encrypted_payload=f"m{i}", created_at=now + timedelta(seconds=i + 1),
        ))
    await db_session.flush()

    # Bob read up to the second message; Carol read nothing.
    await db_session.execute(
        cp.update().where(cp.c.conversation_id == gid, cp.c.user_id == bob_id)
        .values(last_read_at=now + timedelta(seconds=2))
    )
    await db_session.flush()

    def unread_for(listing):
        return next(c["unread_count"] for c in listing if c["id"] == str(gid))

    bob_list = (await client.get("/api/v1/conversations?include_groups=true", headers=_h(bob))).json()
    carol_list = (await client.get("/api/v1/conversations?include_groups=true", headers=_h(carol))).json()
    assert unread_for(bob_list) == 1
    assert unread_for(carol_list) == 3  # system lines never count


async def test_blocked_sender_hidden_in_group(client, db_session, trio):
    (alice, alice_id), (bob, bob_id), (carol, carol_id) = trio
    gid = UUID((await _create_group(client, alice, [bob_id, carol_id])).json()["id"])
    db_session.add(MessageMetadata(
        conversation_id=gid, sender_id=bob_id, message_type=MessageType.text,
        encrypted_payload="from bob", created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    ))
    db_session.add(BlockedUser(blocker_id=carol_id, blocked_id=bob_id))
    await db_session.flush()

    page = (await client.get(f"/api/v1/conversations/{gid}/messages", headers=_h(carol))).json()
    assert "from bob" not in [m["encrypted_payload"] for m in page["messages"]]
    page = (await client.get(f"/api/v1/conversations/{gid}/messages", headers=_h(alice))).json()
    msg = next(m for m in page["messages"] if m["encrypted_payload"] == "from bob")
    # Group history carries the sender's name + key for labelling/decryption.
    assert msg["sender_display_name"] == "Bob" and msg["sender_public_key"]


async def test_system_messages_cannot_be_deleted_for_everyone(client, db_session, trio):
    (alice, _), (_, bob_id), _ = trio
    data = (await _create_group(client, alice, [bob_id])).json()
    r = await client.post(
        f"/api/v1/messages/{data['last_message']['id']}/delete",
        json={"scope": "everyone"}, headers=_h(alice),
    )
    assert r.status_code == 403


async def test_no_calls_in_groups(client, db_session, trio):
    (alice, _), (_, bob_id), _ = trio
    gid = (await _create_group(client, alice, [bob_id])).json()["id"]
    r = await client.post(
        "/api/v1/calls", json={"conversation_id": gid, "callee_id": str(bob_id)}, headers=_h(alice),
    )
    assert r.status_code == 400


# ── WebSocket ────────────────────────────────────────────────────────────────

async def _ws_group(n_members: int = 3) -> tuple[list[UUID], UUID]:
    """Committed group (first user admin) for WebSocket tests."""
    async with TestSessionLocal() as s:
        ids = []
        for i in range(n_members):
            u = User(
                private_number=f"07{os.urandom(4).hex()[:8]}", login_password_hash="x",
                delete_password_hash="x", display_name=f"M{i}", public_key=_key(),
                supports_groups=True,
            )
            s.add(u)
            await s.flush()
            ids.append(u.id)
        conv = Conversation(is_group=True, name="WS group", created_by=ids[0])
        s.add(conv)
        await s.flush()
        await s.execute(insert(cp), [
            {"conversation_id": conv.id, "user_id": uid, "role": "admin" if i == 0 else "member"}
            for i, uid in enumerate(ids)
        ])
        await s.commit()
        return ids, conv.id


def _envelope(member_ids) -> str:
    return json.dumps({"v": 2, "n": "n", "c": "c", "k": {str(m): "wrapped" for m in member_ids}})


def _recv_until(ws, predicate, limit: int = 6) -> dict:
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        if predicate(frame):
            return frame
    raise AssertionError("expected frame not received")


async def test_ws_group_envelope_must_match_members(ws_client):  # noqa: F811
    ids, gid = await _ws_group(3)
    token = create_access_token(ids[0])
    with ws_client.websocket_connect(f"/ws/{gid}?token={token}") as ws:
        # Missing a member → rejected with the message's temp id.
        ws.send_text(json.dumps({
            "type": "message", "message_type": "text",
            "encrypted_payload": _envelope(ids[:2]), "client_temp_id": "t1",
        }))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error" and err["code"] == "group_members_changed"
        assert err["client_temp_id"] == "t1"

        # A v1 (1:1-style) envelope is rejected too.
        ws.send_text(json.dumps({
            "type": "message", "message_type": "text",
            "encrypted_payload": json.dumps({"v": 1, "n": "n", "c": "c"}), "client_temp_id": "t2",
        }))
        assert json.loads(ws.receive_text())["code"] == "group_members_changed"

        # Exactly the members → delivered with sender name + key.
        ws.send_text(json.dumps({
            "type": "message", "message_type": "text",
            "encrypted_payload": _envelope(ids), "client_temp_id": "t3",
        }))
        echo = _recv_until(ws, lambda f: f.get("type") == "message")
        assert echo["client_temp_id"] == "t3"
        assert echo["sender_display_name"] == "M0" and echo["sender_public_key"]


async def test_ws_group_read_moves_watermark_not_read_at(ws_client):  # noqa: F811
    ids, gid = await _ws_group(3)
    async with TestSessionLocal() as s:
        msg = MessageMetadata(
            conversation_id=gid, sender_id=ids[0], message_type=MessageType.text,
            encrypted_payload=_envelope(ids),
        )
        s.add(msg)
        await s.commit()
        msg_id = msg.id

    token = create_access_token(ids[1])
    with ws_client.websocket_connect(f"/ws/{gid}?token={token}") as ws:
        ws.send_text(json.dumps({"type": "read", "message_ids": [str(msg_id)]}))
        frame = _recv_until(ws, lambda f: f.get("type") == "read")
        assert frame["by_user_id"] == str(ids[1])

    async with TestSessionLocal() as s:
        m = await s.get(MessageMetadata, msg_id)
        assert m.read_at is None and m.delivered_at is None  # others' ticks untouched
        watermark = (await s.execute(
            select(cp.c.last_read_at).where(cp.c.conversation_id == gid, cp.c.user_id == ids[1])
        )).scalar_one()
        assert watermark is not None


async def test_ws_removed_member_socket_is_closed(ws_client):  # noqa: F811
    from starlette.websockets import WebSocketDisconnect

    ids, gid = await _ws_group(3)
    admin_token = create_access_token(ids[0])
    victim_token = create_access_token(ids[2])
    with ws_client.websocket_connect(f"/ws/{gid}?token={victim_token}") as ws:
        r = ws_client.delete(
            f"/api/v1/conversations/{gid}/members/{ids[2]}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 204, r.text
        # They get the update (and the system line), then a final 4403 close.
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(5):
                ws.receive_text()
        assert exc.value.code == 4403
