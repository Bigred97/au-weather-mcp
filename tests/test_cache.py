"""Cache layer tests — TTL, isolation, corruption self-heal, concurrency."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest

from au_weather_mcp.cache import Cache


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


async def test_set_then_get_roundtrips(db_path: Path) -> None:
    cache = Cache(db_path)
    await cache.set("k1", b"hello", kind="current")
    got = await cache.get("k1", ttl=timedelta(hours=1))
    assert got == b"hello"


async def test_ttl_expiry_returns_none(db_path: Path) -> None:
    cache = Cache(db_path)
    await cache.set("k1", b"hello", kind="current")
    # ttl=0 → instantly expired
    got = await cache.get("k1", ttl=timedelta(seconds=0))
    assert got is None


async def test_different_keys_dont_collide(db_path: Path) -> None:
    cache = Cache(db_path)
    await cache.set("k1", b"a", kind="current")
    await cache.set("k2", b"b", kind="forecast")
    assert await cache.get("k1", ttl=timedelta(hours=1)) == b"a"
    assert await cache.get("k2", ttl=timedelta(hours=1)) == b"b"


async def test_overwrite_same_key(db_path: Path) -> None:
    cache = Cache(db_path)
    await cache.set("k1", b"v1", kind="current")
    await cache.set("k1", b"v2", kind="current")
    got = await cache.get("k1", ttl=timedelta(hours=1))
    assert got == b"v2"


async def test_clear_specific_kind(db_path: Path) -> None:
    cache = Cache(db_path)
    await cache.set("k1", b"a", kind="current")
    await cache.set("k2", b"b", kind="forecast")
    await cache.clear(kind="current")
    assert await cache.get("k1", ttl=timedelta(hours=1)) is None
    assert await cache.get("k2", ttl=timedelta(hours=1)) == b"b"


async def test_corrupt_db_self_heals(db_path: Path) -> None:
    """A pre-existing cache.db file that isn't a real SQLite database should
    be silently dropped and recreated rather than leaking sqlite3 errors."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is definitely not a sqlite file")
    cache = Cache(db_path)
    # Should NOT raise; should self-heal.
    await cache.set("k1", b"hello", kind="current")
    assert await cache.get("k1", ttl=timedelta(hours=1)) == b"hello"


async def test_concurrent_first_init_is_safe(db_path: Path) -> None:
    """Two concurrent first-use calls used to both run schema setup (a benign
    race). The asyncio.Lock + double-checked init makes this safe."""
    cache = Cache(db_path)
    await asyncio.gather(
        cache.set("a", b"1", kind="current"),
        cache.set("b", b"2", kind="forecast"),
        cache.set("c", b"3", kind="historical"),
    )
    assert await cache.get("a", ttl=timedelta(hours=1)) == b"1"
    assert await cache.get("b", ttl=timedelta(hours=1)) == b"2"
    assert await cache.get("c", ttl=timedelta(hours=1)) == b"3"
