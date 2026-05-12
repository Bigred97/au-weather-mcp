"""Live integration tests against the real Open-Meteo API.

Marked `live` so default pytest runs skip them. Run with `pytest -m live`
before tagging a release. CI runs unit tests only.

These tests are deliberately tolerant of small data drift (Open-Meteo
updates every 15 min) but strict about the response shape and provenance
contract — the trust layer is what we care about most.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from au_weather_mcp import server
from au_weather_mcp.cache import Cache
from au_weather_mcp.client import OpenMeteoClient

pytestmark = pytest.mark.live


@pytest.fixture
async def live_client(tmp_path: Path):
    client = OpenMeteoClient(cache=Cache(tmp_path / "live_cache.db"))
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def patch_server_client(live_client, monkeypatch):
    async def _get_live():
        return live_client
    monkeypatch.setattr(server, "_get_client", _get_live)


async def test_latest_sydney_returns_current_observation():
    """The flagship use case — 'what's the weather in Sydney right now?'"""
    resp = await server.latest("sydney")
    assert resp.location_id == "sydney"
    assert resp.location_name == "Sydney"
    assert resp.state == "NSW"
    assert resp.current is not None
    # Sydney temperatures are realistically -2 to 45 °C across the year
    assert -2 <= resp.current.temperature_c <= 45, (
        f"Sydney temp {resp.current.temperature_c} outside plausible range"
    )
    # Humidity must be 0-100
    if resp.current.relative_humidity_pct is not None:
        assert 0 <= resp.current.relative_humidity_pct <= 100
    # Provenance must be populated
    assert "open-meteo.com" in resp.source_url
    assert "CC BY 4.0" in resp.attribution
    assert resp.server_version


async def test_latest_all_eight_capitals_return_real_data():
    """Smoke test that every capital city returns plausible data on a single
    minimal-filter query. Mirrors the per-dataflow check in abs-mcp."""
    capitals = ["sydney", "melbourne", "brisbane", "perth",
                "adelaide", "hobart", "darwin", "canberra"]
    for capital in capitals:
        resp = await server.latest(capital)
        assert resp.current is not None, f"{capital} returned no current observation"
        # Australian capitals span tropical to sub-alpine; wide bounds
        assert -10 <= resp.current.temperature_c <= 50, (
            f"{capital} temp {resp.current.temperature_c} is implausible"
        )


async def test_get_weather_historical_sydney_2020():
    """Open-Meteo's archive endpoint should return real 2020 Sydney data."""
    resp = await server.get_weather(
        "sydney",
        start_date="2020-01-01",
        end_date="2020-01-07",
        granularity="daily",
    )
    assert resp.daily is not None
    assert len(resp.daily) == 7
    # Sydney summer Jan should be warm — sanity check
    jan_temps = [d.temperature_max_c for d in resp.daily if d.temperature_max_c is not None]
    assert jan_temps
    avg = sum(jan_temps) / len(jan_temps)
    assert 18 <= avg <= 40, f"Sydney Jan 2020 average max {avg} implausible"


async def test_get_weather_hourly_returns_24_rows_for_one_day():
    """One-day hourly query should return exactly 24 records."""
    # Use a recent historical date (>5 days ago for archive routing)
    from datetime import date, timedelta
    one_week_ago = (date.today() - timedelta(days=14)).isoformat()
    resp = await server.get_weather(
        "sydney",
        start_date=one_week_ago,
        end_date=one_week_ago,
        granularity="hourly",
    )
    assert resp.hourly is not None
    assert len(resp.hourly) == 24, f"Expected 24 hourly rows, got {len(resp.hourly)}"


async def test_describe_location_real_data():
    detail = await server.describe_location("brisbane")
    assert detail.timezone == "Australia/Brisbane"
    assert -28 <= detail.latitude <= -27
    assert 152 <= detail.longitude <= 154


async def test_search_locations_natural_language():
    """Common natural-language phrases should resolve correctly."""
    results = await server.search_locations("tropical north", limit=3)
    ids = [r.id for r in results]
    assert any(i in ids for i in ("cairns", "townsville", "darwin", "broome"))


async def test_cache_warm_then_cold_latency():
    """Warm cache should be much faster than cold. This is a soft assertion
    — we just want to confirm caching is working."""
    import time
    # Cold (first call)
    t0 = time.perf_counter()
    await server.latest("perth")
    cold_ms = (time.perf_counter() - t0) * 1000
    # Warm (second call to same endpoint)
    t0 = time.perf_counter()
    await server.latest("perth")
    warm_ms = (time.perf_counter() - t0) * 1000
    assert warm_ms < cold_ms, f"Warm ({warm_ms:.0f}ms) should be faster than cold ({cold_ms:.0f}ms)"


async def test_response_includes_full_attribution():
    """Every response must carry the CC-BY 4.0 attribution string."""
    resp = await server.latest("melbourne")
    assert "Open-Meteo" in resp.attribution
    assert "CC BY 4.0" in resp.attribution
    assert "Bureau of Meteorology" in resp.attribution


# ---------- v0.4.0: live tests for new endpoints ----------

async def test_air_quality_sydney_returns_real_data():
    """End-to-end air-quality for Sydney."""
    resp = await server.air_quality("sydney")
    assert resp.location_id == "sydney"
    assert resp.location_resolution == "curated"
    assert resp.current is not None
    # Sydney PM2.5 routinely 5-30 µg/m³ ambient; reject implausibles
    if resp.current.pm2_5_ugm3 is not None:
        assert 0 <= resp.current.pm2_5_ugm3 <= 500
    if resp.current.european_aqi is not None:
        assert 0 <= resp.current.european_aqi <= 500
    # AQI label always populated when AQI is
    if resp.current.european_aqi is not None:
        assert resp.current.european_aqi_label is not None
    # Trust contract
    assert "open-meteo.com" in resp.source_url
    assert "CC BY 4.0" in resp.attribution
    assert resp.server_version


async def test_air_quality_via_postcode_carries_osm_attribution():
    """Postcode-resolved air quality should keep the OSM suffix that
    weather responses carry — Nominatim still in the resolution chain."""
    resp = await server.air_quality("2026")  # Bondi Beach postcode
    assert resp.location_resolution == "postcode"
    assert resp.current is not None
    assert "OpenStreetMap" in resp.attribution
    assert "ODbL" in resp.attribution


async def test_compare_locations_four_capitals():
    """Compare current weather across all four largest capitals."""
    resp = await server.compare_locations(["sydney", "melbourne", "brisbane", "perth"])
    assert resp.metric == "current"
    assert len(resp.locations) == 4
    ids = [r.location_id for r in resp.locations]
    assert set(ids) == {"sydney", "melbourne", "brisbane", "perth"}
    for row in resp.locations:
        assert row.error is None, f"{row.location_id} errored: {row.error}"
        assert row.current is not None
        # Each location's temperature should be in the plausible AU range
        if row.current.temperature_c is not None:
            assert -10 <= row.current.temperature_c <= 50


async def test_compare_locations_handles_mix_of_resolution_paths():
    """One curated + one postcode + one geocoded — all in one comparison."""
    resp = await server.compare_locations(["sydney", "2026", "Byron Bay"])
    assert len(resp.locations) == 3
    sources = {r.location_resolution for r in resp.locations}
    assert sources == {"curated", "postcode", "geocoded"}
    for row in resp.locations:
        assert row.error is None, f"row {row.location_input!r} errored: {row.error}"
        assert row.current is not None


async def test_compare_locations_isolates_bad_input():
    """One garbage input should NOT take down the whole call — it surfaces
    on its own row's error field while the others succeed."""
    resp = await server.compare_locations(["sydney", "Xenoluthia_fake_place_xyz"])
    assert len(resp.locations) == 2
    # Sydney should succeed
    sydney_row = next(r for r in resp.locations if r.location_id == "sydney")
    assert sydney_row.error is None
    assert sydney_row.current is not None
    # The fake place should report an error
    bad_row = next(r for r in resp.locations if r.location_input == "Xenoluthia_fake_place_xyz")
    assert bad_row.error is not None
    assert bad_row.current is None
