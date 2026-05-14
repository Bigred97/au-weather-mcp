"""HTTP client tests — uses httpx.MockTransport so no live calls."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from au_weather_mcp.cache import Cache
from au_weather_mcp.client import OpenMeteoClient, OpenMeteoError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


def _mock(handler):
    return httpx.MockTransport(handler)


async def test_forecast_returns_parsed_json(db_path: Path) -> None:
    payload = {
        "latitude": -33.86,
        "longitude": 151.21,
        "timezone": "Australia/Sydney",
        "current": {
            "time": "2026-05-12T11:30",
            "temperature_2m": 19.7,
            "relative_humidity_2m": 67,
            "wind_speed_10m": 18.4,
        },
    }
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        got = await c.forecast(
            latitude=-33.86,
            longitude=151.21,
            timezone="Australia/Sydney",
            current=["temperature_2m", "relative_humidity_2m", "wind_speed_10m"],
        )
    assert got["current"]["temperature_2m"] == 19.7


async def test_4xx_with_json_body_surfaces_reason(db_path: Path) -> None:
    """Open-Meteo returns 400 with `{"reason": "..."}` on bad input.
    We surface that reason so the user gets an actionable error."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})
    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        with pytest.raises(OpenMeteoError, match="Latitude must be in range"):
            await c.forecast(latitude=999, longitude=0, timezone="Australia/Sydney")


async def test_network_error_wraps_as_openmeteo_error(db_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        with pytest.raises(OpenMeteoError, match="request failed"):
            await c.forecast(latitude=-33.86, longitude=151.21, timezone="Australia/Sydney")


async def test_cache_hit_skips_http(db_path: Path) -> None:
    """Second identical call should not hit the network."""
    call_count = {"n": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"latitude": 0, "longitude": 0, "current": {"time": "x"}})
    cache = Cache(db_path)
    async with OpenMeteoClient(cache=cache, transport=_mock(handler)) as c:
        await c.forecast(latitude=-33.86, longitude=151.21, timezone="Australia/Sydney",
                         current=["temperature_2m"])
        await c.forecast(latitude=-33.86, longitude=151.21, timezone="Australia/Sydney",
                         current=["temperature_2m"])
    assert call_count["n"] == 1, "Second call should have hit cache, not network"


async def test_url_includes_all_params(db_path: Path) -> None:
    """The cache key is the URL — must include every distinguishing param so
    different queries don't accidentally share a cache entry."""
    captured = {}
    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"latitude": 0, "longitude": 0})
    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        await c.archive(
            latitude=-33.86,
            longitude=151.21,
            timezone="Australia/Sydney",
            start_date="2020-01-01",
            end_date="2020-01-07",
            daily=["temperature_2m_max", "precipitation_sum"],
        )
    url = captured["url"]
    assert "start_date=2020-01-01" in url
    assert "end_date=2020-01-07" in url
    assert "temperature_2m_max" in url
    assert "precipitation_sum" in url


async def test_unparseable_body_raises_clean_error(db_path: Path) -> None:
    """If Open-Meteo ever returns HTML or garbage, we wrap as OpenMeteoError
    rather than leaking json.JSONDecodeError."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>",
                              headers={"content-type": "text/html"})
    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        with pytest.raises(OpenMeteoError, match="parse"):
            await c.forecast(latitude=-33.86, longitude=151.21, timezone="Australia/Sydney")


# ─── stale-fallback graceful degradation (CLAUDE.md quality dim #4) ──────

async def _prime_stale_cache(
    db_path: Path, url: str, payload: bytes, age_hours: float
) -> None:
    """Put `payload` into the cache as if it was fetched `age_hours` ago.
    Used to test the stale-fallback path: a regular cache.get() with a normal
    TTL will miss this row (because cached_at is older than the TTL window),
    but cache.get_stale() will still return it.
    """
    import time as _time

    import aiosqlite

    from au_weather_mcp.cache import Cache

    cache = Cache(db_path)
    await cache._ensure_init()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO http_cache (cache_key, payload, cached_at, kind) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, cached_at=excluded.cached_at",
            (url, payload, _time.time() - age_hours * 3600, "forecast"),
        )
        await conn.commit()


async def test_stale_fallback_serves_cached_payload_on_5xx(db_path: Path) -> None:
    """When upstream Open-Meteo returns 5xx and we have a cached payload past
    its TTL, serve the cached payload and mark the response as stale. Agents
    continue reasoning rather than crashing."""
    from au_weather_mcp.client import get_stale_signal, reset_stale_signal

    payload = {
        "latitude": -33.86,
        "longitude": 151.21,
        "timezone": "Australia/Sydney",
        "current": {"time": "2026-05-12T11:30", "temperature_2m": 19.7},
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    # The URL must match what client.forecast() builds. Use a fixed query.
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=-33.86&longitude=151.21"
        "&timezone=Australia%2FSydney&current=temperature_2m"
    )
    # Prime a 48h-old cache entry — past the 1h forecast TTL, so cache.get()
    # misses but cache.get_stale() still returns it.
    await _prime_stale_cache(db_path, url, payload_bytes, age_hours=48)

    reset_stale_signal()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        got = await c.forecast(
            latitude=-33.86,
            longitude=151.21,
            timezone="Australia/Sydney",
            current=["temperature_2m"],
        )
    assert got["current"]["temperature_2m"] == 19.7, "fallback payload must round-trip"
    stale, reason = get_stale_signal()
    assert stale is True, "stale flag must be set after 5xx fallback"
    assert reason and "503" in reason, f"stale_reason should mention the 5xx: {reason}"
    assert "minute" in reason.lower(), f"stale_reason should report age: {reason}"


async def test_stale_fallback_serves_cached_on_request_error(db_path: Path) -> None:
    """Same as 5xx test but for httpx.RequestError (DNS / connection refused / etc.)."""
    from au_weather_mcp.client import get_stale_signal, reset_stale_signal

    payload = {
        "latitude": -33.86,
        "longitude": 151.21,
        "timezone": "Australia/Sydney",
        "current": {"time": "2026-05-12T11:30", "temperature_2m": 19.7},
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=-33.86&longitude=151.21"
        "&timezone=Australia%2FSydney&current=temperature_2m"
    )
    await _prime_stale_cache(db_path, url, payload_bytes, age_hours=48)

    reset_stale_signal()

    def raise_request_error(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    async with OpenMeteoClient(
        cache=Cache(db_path), transport=_mock(raise_request_error)
    ) as c:
        got = await c.forecast(
            latitude=-33.86,
            longitude=151.21,
            timezone="Australia/Sydney",
            current=["temperature_2m"],
        )
    assert got["current"]["temperature_2m"] == 19.7
    stale, reason = get_stale_signal()
    assert stale is True
    assert reason and "ConnectError" in reason


async def test_raises_when_no_stale_cache_to_fall_back_to(db_path: Path) -> None:
    """Empty cache + upstream 5xx → still raises OpenMeteoError (original
    behaviour when there's nothing to gracefully degrade to)."""
    from au_weather_mcp.client import reset_stale_signal

    reset_stale_signal()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    async with OpenMeteoClient(cache=Cache(db_path), transport=_mock(handler)) as c:
        with pytest.raises(OpenMeteoError, match="503"):
            await c.forecast(
                latitude=-33.86, longitude=151.21, timezone="Australia/Sydney"
            )


async def test_cache_get_stale_returns_payload_and_timestamp(db_path: Path) -> None:
    """Cache.get_stale() returns (payload, cached_at) regardless of TTL —
    the building block for client's stale-fallback path."""
    from datetime import timedelta

    cache = Cache(db_path)
    await cache.set("https://example.org/x", b"hello", kind="forecast")
    # Normal `get` with a tiny TTL should miss
    fresh = await cache.get("https://example.org/x", ttl=timedelta(seconds=0))
    assert fresh is None
    # `get_stale` should return regardless of TTL
    stale = await cache.get_stale("https://example.org/x")
    assert stale is not None
    payload, cached_at = stale
    assert payload == b"hello"
    assert cached_at > 0
    # Non-existent key → None
    miss = await cache.get_stale("https://example.org/missing")
    assert miss is None
