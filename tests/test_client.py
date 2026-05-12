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
