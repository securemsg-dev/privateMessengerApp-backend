"""Per-request perf logging and the Redis cache helper."""
import asyncio
import re

import pytest

from app.core import cache
from app.core.config import settings
from tests.conftest import register_and_login

PERF_LINE = re.compile(
    r"\[perf\] (?P<method>\S+) (?P<path>\S+) (?P<status>\d+) [\d.]+ms "
    r"db=(?P<db>\d+) \(stmts=(?P<stmts>\d+) tx=(?P<tx>\d+)\) "
    r"cache=(?P<hits>\d+)/(?P<misses>\d+)"
)


def _perf_lines(caplog):
    return [
        m for m in (PERF_LINE.search(r.getMessage()) for r in caplog.records) if m
    ]


# ── Perf logging ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_perf_line_counts_db_round_trips(client, caplog, monkeypatch):
    auth = await register_and_login(client)
    monkeypatch.setattr(settings, "PERF_LOGGING_ENABLED", True)
    caplog.set_level("INFO", logger="app.perf")

    resp = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {auth['tokens']['access_token']}"},
    )
    assert resp.status_code == 200

    lines = _perf_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET"
    assert line["path"] == "/api/v1/users/me"
    assert line["status"] == "200"
    # At least the auth dependency's user lookup.
    assert int(line["stmts"]) >= 1
    assert int(line["db"]) == int(line["stmts"]) + int(line["tx"])


@pytest.mark.asyncio
async def test_perf_line_uses_route_template_not_raw_ids(client, caplog, monkeypatch):
    auth = await register_and_login(client)
    monkeypatch.setattr(settings, "PERF_LOGGING_ENABLED", True)
    caplog.set_level("INFO", logger="app.perf")

    conv_id = "11111111-2222-3333-4444-555555555555"
    await client.get(
        f"/api/v1/conversations/{conv_id}/messages",
        headers={"Authorization": f"Bearer {auth['tokens']['access_token']}"},
    )

    lines = _perf_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["path"] == "/api/v1/conversations/{conversation_id}/messages"
    perf_logged = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "app.perf"
    )
    assert conv_id not in perf_logged


@pytest.mark.asyncio
async def test_perf_logging_is_off_by_default_and_skips_health(client, caplog, monkeypatch):
    caplog.set_level("INFO", logger="app.perf")
    await client.get("/api/v1/users/me")  # 401/403 — still a request
    assert _perf_lines(caplog) == []

    monkeypatch.setattr(settings, "PERF_LOGGING_ENABLED", True)
    await client.get("/health")
    assert _perf_lines(caplog) == []


# ── Cache helper ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cached_loads_once_then_serves_from_redis(fake_redis):
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return {"members": ["a", "b"]}

    first = await cache.cached(fake_redis, "t:1", 60, loader)
    second = await cache.cached(fake_redis, "t:1", 60, loader)
    assert first == second == {"members": ["a", "b"]}
    assert calls == 1
    # Stored under the namespaced key, with a TTL.
    assert 0 < await fake_redis.ttl(cache.KEY_PREFIX + "t:1") <= 60


@pytest.mark.asyncio
async def test_delete_invalidates(fake_redis):
    await cache.cache_set(fake_redis, "t:2", [1, 2], 60)
    assert await cache.cache_get(fake_redis, "t:2") == [1, 2]
    await cache.cache_delete(fake_redis, "t:2")
    assert await cache.cache_get(fake_redis, "t:2") is None


@pytest.mark.asyncio
async def test_none_is_never_cached(fake_redis):
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return None

    assert await cache.cached(fake_redis, "t:3", 60, loader) is None
    assert await cache.cached(fake_redis, "t:3", 60, loader) is None
    assert calls == 2


@pytest.mark.asyncio
async def test_disabled_flag_bypasses_reads_and_writes_but_not_deletes(fake_redis, monkeypatch):
    await cache.cache_set(fake_redis, "t:4", "old", 60)

    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    assert await cache.cache_get(fake_redis, "t:4") is None
    await cache.cache_set(fake_redis, "t:5", "x", 60)
    assert await fake_redis.get(cache.KEY_PREFIX + "t:5") is None
    # Invalidation still runs, so the stale entry can't resurface later.
    await cache.cache_delete(fake_redis, "t:4")

    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    assert await cache.cache_get(fake_redis, "t:4") is None


class _BrokenRedis:
    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    async def set(self, *a, **k):
        raise ConnectionError("redis down")

    async def delete(self, *a, **k):
        raise ConnectionError("redis down")


class _HungRedis:
    async def get(self, *a, **k):
        await asyncio.sleep(30)

    set = delete = get


@pytest.mark.asyncio
async def test_redis_failure_falls_through_to_loader():
    async def loader():
        return "from-db"

    redis = _BrokenRedis()
    assert await cache.cached(redis, "t:6", 60, loader) == "from-db"
    await cache.cache_delete(redis, "t:6")  # must not raise


@pytest.mark.asyncio
async def test_hung_redis_times_out_instead_of_blocking(monkeypatch):
    monkeypatch.setattr(settings, "CACHE_OP_TIMEOUT_SECONDS", 0.05)

    async def loader():
        return "from-db"

    result = await asyncio.wait_for(
        cache.cached(_HungRedis(), "t:7", 60, loader), timeout=2,
    )
    assert result == "from-db"


@pytest.mark.asyncio
async def test_corrupt_entry_is_a_miss(fake_redis):
    await fake_redis.set(cache.KEY_PREFIX + "t:8", b"{not json")
    assert await cache.cache_get(fake_redis, "t:8") is None
