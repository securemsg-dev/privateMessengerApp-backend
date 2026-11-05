"""
Presence: online / last seen.

Covers the manager's transition logic (fake sockets), the presence service
(visibility rules, fan-out to 1:1 partners only, blocks, hidden users), the
?presence=1 snapshot on conversation sockets, the user-socket presence frame,
and the PATCH /users/me privacy toggle.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import fakeredis
import pytest
from sqlalchemy import insert

from app.core.security import create_access_token
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import conversation_participants
from app.db.models.user import User
from app.services import presence
from app.websocket.manager import ConnectionManager
from tests.conftest import (
    TestSessionLocal,
    auth_header,
    create_conversation_with_participants,
    create_user_directly,
    register_and_login,
)
from tests.test_websocket import (  # noqa: F401 — fixtures
    _setup_user_and_conversation,
    ws_app,
    ws_client,
)


# ── Manager transitions ───────────────────────────────────────────────────────

class FakeWS:
    async def accept(self) -> None:
        return None

    async def send_text(self, _data: str) -> None:
        return None


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_manager_reports_only_aggregate_transitions():
    m = ConnectionManager()
    events: list[tuple[str, bool]] = []

    async def hook(uid: str, online: bool) -> None:
        events.append((uid, online))

    m.set_presence_hook(hook)
    phone, tablet = FakeWS(), FakeWS()

    await m.connect_user(phone, "u1")      # offline → online
    await m.connect_user(tablet, "u1")     # still online: no event
    m.set_user_socket_active(phone, "u1", False)   # tablet still active: no event
    m.set_user_socket_active(tablet, "u1", False)  # now nobody active → offline
    m.set_user_socket_active(tablet, "u1", True)   # back online
    await m.disconnect_user(tablet, "u1")  # phone is background → offline
    await m.disconnect_user(phone, "u1")   # already offline: no event
    await _settle()

    assert events == [("u1", True), ("u1", False), ("u1", True), ("u1", False)]


async def test_is_user_active_semantics():
    m = ConnectionManager()
    ws = FakeWS()
    await m.connect_user(ws, "u1")
    assert m.is_user_active("u1")
    m.set_user_socket_active(ws, "u1", False)
    assert not m.is_user_active("u1")      # backgrounded app → push again
    assert m.is_user_connected("u1")       # …but still reachable for live updates
    await m.disconnect_user(ws, "u1")

    # Old build with only a conversation socket: still counts as active.
    conv_ws = FakeWS()
    await m.connect(conv_ws, "c1", "u2")
    assert m.is_user_active("u2")
    assert m.active_user_ids() == []       # no user socket → no presence key refresh


async def test_presence_frames_only_reach_opted_in_sockets():
    m = ConnectionManager()
    sent: dict[str, list[str]] = {"old": [], "new": []}

    class Rec(FakeWS):
        def __init__(self, name: str) -> None:
            self.name = name

        async def send_text(self, data: str) -> None:
            sent[self.name].append(data)

    await m.connect(Rec("old"), "c1", "a")
    await m.connect(Rec("new"), "c1", "b", wants_presence=True)
    await m.broadcast_to_conversation("c1", json.dumps({"type": "presence", "user_id": "x", "online": True}))
    await m.broadcast_to_conversation("c1", json.dumps({"type": "message", "encrypted_payload": "presence"}))

    assert len(sent["old"]) == 1 and json.loads(sent["old"][0])["type"] == "message"
    assert [json.loads(d)["type"] for d in sent["new"]] == ["presence", "message"]


# ── Presence service ─────────────────────────────────────────────────────────

@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=False)


@asynccontextmanager
async def _shared_session(db):
    yield db


async def _published(mock: AsyncMock) -> list[tuple[str, dict]]:
    return [(c.args[1], c.args[2]) for c in mock.await_args_list]


async def test_online_offline_fan_out_to_one_to_one_partners(db_session, redis):
    alice = await create_user_directly(db_session)
    bob = await create_user_directly(db_session)
    carol = await create_user_directly(db_session)
    dm = await create_conversation_with_participants(db_session, [alice.id, bob.id])
    await create_conversation_with_participants(db_session, [alice.id, bob.id, carol.id])  # group

    with patch.object(presence, "AsyncSessionLocal", lambda: _shared_session(db_session)), \
         patch.object(presence.manager, "publish", new=AsyncMock()) as pub:
        await presence.on_presence_change(redis, str(alice.id), True)
        assert await redis.exists(f"presence:{alice.id}")
        online_events = await _published(pub)

        pub.reset_mock()
        await presence.on_presence_change(redis, str(alice.id), False)
        offline_events = await _published(pub)

    # Only the 1:1 chat gets presence — never the group.
    assert [c for c, _ in online_events] == [str(dm.id)]
    assert online_events[0][1] == {
        "type": "presence", "user_id": str(alice.id), "online": True, "last_seen": None,
    }
    assert not await redis.exists(f"presence:{alice.id}")
    refreshed = await db_session.get(User, alice.id)
    assert refreshed.last_seen_at is not None
    assert offline_events[0][1]["online"] is False
    assert offline_events[0][1]["last_seen"] is not None


async def test_hidden_user_always_appears_offline(db_session, redis):
    alice = await create_user_directly(db_session)
    bob = await create_user_directly(db_session)
    await create_conversation_with_participants(db_session, [alice.id, bob.id])
    alice.show_online_status = False
    alice.last_seen_at = datetime.now(timezone.utc)
    await db_session.flush()
    await redis.set(f"presence:{alice.id}", "1")

    with patch.object(presence.manager, "publish", new=AsyncMock()) as pub:
        await presence.publish_presence(db_session, redis, alice)
    event = pub.await_args.args[2]
    assert event["online"] is False and event["last_seen"] is None

    snap = await presence.snapshot_for(db_session, redis, bob.id, alice.id)
    assert snap["online"] is False and snap["last_seen"] is None


async def test_blocked_pairs_never_see_presence(db_session, redis):
    alice = await create_user_directly(db_session)
    bob = await create_user_directly(db_session)
    dave = await create_user_directly(db_session)
    await create_conversation_with_participants(db_session, [alice.id, bob.id])
    dm_dave = await create_conversation_with_participants(db_session, [alice.id, dave.id])
    db_session.add(BlockedUser(blocker_id=bob.id, blocked_id=alice.id))  # bob blocked alice
    await db_session.flush()
    await redis.set(f"presence:{alice.id}", "1")

    with patch.object(presence.manager, "publish", new=AsyncMock()) as pub:
        await presence.publish_presence(db_session, redis, alice)
    assert [c.args[1] for c in pub.await_args_list] == [str(dm_dave.id)]

    # Either direction hides it in snapshots too.
    assert (await presence.snapshot_for(db_session, redis, bob.id, alice.id))["online"] is False
    assert (await presence.snapshot_for(db_session, redis, alice.id, bob.id))["online"] is False
    assert (await presence.snapshot_for(db_session, redis, dave.id, alice.id))["online"] is True


# ── WebSocket integration ────────────────────────────────────────────────────

async def _commit_last_seen(user_id, when, *, visible: bool = True) -> None:
    async with TestSessionLocal() as s:
        u = await s.get(User, user_id)
        u.last_seen_at = when
        u.show_online_status = visible
        await s.commit()


async def _add_peer(conv_id, number: str):
    async with TestSessionLocal() as s:
        peer = User(private_number=number, login_password_hash="x", delete_password_hash="x")
        s.add(peer)
        await s.flush()
        await s.execute(insert(conversation_participants).values(conversation_id=conv_id, user_id=peer.id))
        await s.commit()
        return peer.id


async def test_presence_snapshot_on_opt_in(ws_client):  # noqa: F811
    me, conv_id = await _setup_user_and_conversation("+60310000001")
    peer = await _add_peer(conv_id, "0310000002")
    seen = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)
    await _commit_last_seen(peer, seen)
    token = create_access_token(me)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}&presence=1") as ws:
        snap = json.loads(ws.receive_text())
    assert snap["type"] == "presence"
    assert snap["user_id"] == str(peer)
    assert snap["online"] is False
    assert snap["last_seen"].startswith("2026-11-01T09:30")

    # Hidden peer → nothing revealed.
    await _commit_last_seen(peer, seen, visible=False)
    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}&presence=1") as ws:
        snap = json.loads(ws.receive_text())
    assert snap["online"] is False and snap["last_seen"] is None


async def test_no_snapshot_without_opt_in(ws_client):  # noqa: F811
    """Old builds don't ask, so their first frame is unchanged."""
    me, conv_id = await _setup_user_and_conversation("+60310000003", "+60310000004")
    token = create_access_token(me)
    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["detail"] == "Invalid JSON"


async def test_user_socket_presence_frame(ws_client):  # noqa: F811
    me, _ = await _setup_user_and_conversation("+60310000005")
    token = create_access_token(me)
    with ws_client.websocket_connect(f"/ws/user?token={token}") as ws:
        ws.send_text(json.dumps({"type": "presence", "state": "sleeping"}))
        assert json.loads(ws.receive_text())["detail"] == "Invalid presence state"
        # A valid frame produces no reply: the next frame is the barrier's error.
        ws.send_text(json.dumps({"type": "presence", "state": "background"}))
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["detail"] == "Invalid JSON"


# ── Privacy toggle ───────────────────────────────────────────────────────────

async def test_show_online_status_toggle(client, fake_redis):
    alice = await register_and_login(client)
    headers = auth_header(alice["tokens"]["access_token"])

    me = await client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["show_online_status"] is True

    with patch("app.api.v1.endpoints.users.publish_presence", new=AsyncMock()) as pub:
        resp = await client.patch("/api/v1/users/me", json={"show_online_status": False}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["show_online_status"] is False
        assert pub.await_count == 1  # open chats told immediately

        # Unchanged value → no re-publish.
        resp = await client.patch("/api/v1/users/me", json={"show_online_status": False}, headers=headers)
        assert pub.await_count == 1
