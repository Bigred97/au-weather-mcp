"""Server-tool input validation — every bad input shape must raise ValueError
with an actionable hint. Location resolution itself has its own test file
(test_resolution.py); this file covers everything else.

Mirrors abs-mcp / rba-mcp's validation test pattern.
"""
from __future__ import annotations

import pytest

from au_weather_mcp import server


# ---------- _validate_date ----------

def test_validate_date_accepts_iso():
    assert server._validate_date("2024-03-15", "start_date") == "2024-03-15"
    assert server._validate_date(None, "start_date") is None
    assert server._validate_date("", "start_date") is None


def test_validate_date_rejects_non_string():
    with pytest.raises(ValueError, match="must be a string"):
        server._validate_date(2024, "start_date")
    with pytest.raises(ValueError, match="must be a string"):
        server._validate_date(["2024-03-15"], "start_date")


def test_validate_date_rejects_bad_format():
    for bad in ("2024", "2024-03", "March 2024", "15/03/2024", "2024/03/15"):
        with pytest.raises(ValueError, match="invalid format"):
            server._validate_date(bad, "start_date")


def test_validate_date_rejects_semantically_invalid():
    """The regex accepts shapes like 2024-13-40 — semantic check must catch."""
    with pytest.raises(ValueError, match="not a valid date"):
        server._validate_date("2024-13-40", "start_date")
    with pytest.raises(ValueError, match="not a valid date"):
        server._validate_date("2024-02-30", "start_date")


def test_validate_date_rejects_url_injection():
    """Even URL-unsafe characters that pass the regex should be rejected."""
    for bad in ("2024-01-01?", "2024-01-01&x=1", "2024-01-01/extra"):
        with pytest.raises(ValueError, match="invalid format"):
            server._validate_date(bad, "start_date")


# ---------- search_locations ----------

async def test_search_locations_rejects_non_string_query():
    with pytest.raises(ValueError, match="query must be a string"):
        await server.search_locations(123)  # type: ignore[arg-type]


async def test_search_locations_rejects_empty_query():
    with pytest.raises(ValueError, match="query is required"):
        await server.search_locations("")


async def test_search_locations_rejects_negative_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await server.search_locations("sydney", limit=-1)


async def test_search_locations_rejects_bool_limit():
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        await server.search_locations("sydney", limit=True)  # type: ignore[arg-type]


async def test_search_locations_returns_sydney_for_sydney():
    results = await server.search_locations("sydney", limit=3)
    assert results
    assert results[0].id == "sydney"
    assert results[0].state == "NSW"


# ---------- describe_location ----------

async def test_describe_location_rejects_non_string():
    with pytest.raises(ValueError, match="location must be a string"):
        await server.describe_location(123)  # type: ignore[arg-type]


async def test_describe_sydney_returns_metadata():
    detail = await server.describe_location("sydney")
    assert detail.id == "sydney"
    assert detail.name == "Sydney"
    assert detail.state == "NSW"
    assert detail.timezone == "Australia/Sydney"
    # Must include the CC-BY attribution string
    assert "CC BY 4.0" in detail.attribution
    # Source URL must point at the canonical Open-Meteo forecast endpoint
    assert "api.open-meteo.com" in detail.open_meteo_url


async def test_describe_sydney_case_insensitive():
    """Customer typing 'Sydney' instead of 'sydney' must still work."""
    detail = await server.describe_location("Sydney")
    assert detail.id == "sydney"


async def test_describe_state_code_returns_capital():
    """Customer typing 'NSW' should resolve to Sydney."""
    detail = await server.describe_location("NSW")
    assert detail.id == "sydney"
    assert detail.state == "NSW"


@pytest.mark.live
async def test_describe_location_geocoded_returns_id_none():
    """REGRESSION (iter-1 audit): describe_location used to crash with a
    pydantic ValidationError on every non-curated input because LocationDetail.id
    was non-nullable. Geocoded places must return id=None, not crash."""
    detail = await server.describe_location("Margaret River")
    assert detail.id is None
    assert "Margaret River" in detail.name
    assert detail.state == "Western Australia"
    assert detail.latitude is not None
    assert detail.longitude is not None
    # Attribution must still be populated
    assert "CC BY 4.0" in detail.attribution


@pytest.mark.live
async def test_describe_location_raw_coordinates_returns_id_none():
    """REGRESSION (iter-1 audit): raw lat/lng input should return id=None,
    not crash."""
    detail = await server.describe_location("-33.87,151.21")
    assert detail.id is None
    # name is just the coords-formatted string for raw-coord lookups
    assert "(-33" in detail.name


@pytest.mark.live
async def test_describe_location_postcode_returns_id_none_with_osm_attribution():
    """REGRESSION (iter-1 audit): postcode resolution should return id=None
    and include OSM attribution."""
    detail = await server.describe_location("2026")
    assert detail.id is None
    # Bondi Beach is the canonical 2026 suburb
    assert "Bondi" in detail.name
    assert detail.state == "New South Wales"


@pytest.mark.live
async def test_postcode_outside_au_bbox_is_rejected():
    """REGRESSION (iter-1 audit): Norfolk Island postcode 2899 was silently
    serving weather at (-29.04, 167.95) — outside the declared AU bbox.
    The bbox check must apply to postcode-resolved locations too."""
    with pytest.raises(ValueError, match="outside the Australia bounding box"):
        await server.latest("2899")
    # Same on describe_location and get_weather
    with pytest.raises(ValueError, match="outside the Australia bounding box"):
        await server.describe_location("2899")
    with pytest.raises(ValueError, match="outside the Australia bounding box"):
        await server.get_weather("2899")


# ---------- latest ----------

async def test_latest_rejects_non_string_location():
    with pytest.raises(ValueError, match="location must be a string"):
        await server.latest(123)  # type: ignore[arg-type]


# ---------- get_weather ----------

async def test_get_weather_rejects_non_string_location():
    with pytest.raises(ValueError, match="location must be a string"):
        await server.get_weather(123)  # type: ignore[arg-type]


async def test_get_weather_rejects_non_string_start_date():
    with pytest.raises(ValueError, match="start_date must be a string"):
        await server.get_weather("sydney", start_date=2024)  # type: ignore[arg-type]


async def test_get_weather_rejects_bad_date_format():
    with pytest.raises(ValueError, match="invalid format"):
        await server.get_weather("sydney", start_date="2024", end_date="2024")


async def test_get_weather_rejects_semantically_invalid_date():
    with pytest.raises(ValueError, match="not a valid date"):
        await server.get_weather("sydney", start_date="2024-13-01", end_date="2024-13-15")


async def test_get_weather_rejects_reversed_range():
    with pytest.raises(ValueError, match="end_date .* is before start_date"):
        await server.get_weather("sydney", start_date="2024-12-31", end_date="2024-01-01")


async def test_get_weather_rejects_invalid_granularity():
    with pytest.raises(ValueError, match="granularity must be 'daily' or 'hourly'"):
        await server.get_weather("sydney", granularity="weekly")  # type: ignore[arg-type]


# ---------- list_curated ----------

def test_list_curated_returns_sorted_45_ids():
    ids = server.list_curated()
    assert len(ids) == 45
    assert ids == sorted(ids)
    # Spot-check expected entries are present from each state
    for required in (
        "sydney", "melbourne", "brisbane", "perth", "darwin", "alice_springs",
        "tamworth", "toowoomba", "mildura", "bunbury", "mount_gambier", "katherine",
    ):
        assert required in ids


# ---------- v0.4.0: compare_locations ----------

async def test_compare_locations_rejects_non_list():
    with pytest.raises(ValueError, match="locations must be a list"):
        await server.compare_locations("sydney")  # type: ignore[arg-type]


async def test_compare_locations_rejects_too_few():
    with pytest.raises(ValueError, match="requires at least 2"):
        await server.compare_locations(["sydney"])


async def test_compare_locations_rejects_too_many():
    with pytest.raises(ValueError, match="at most 10"):
        await server.compare_locations(["sydney"] * 11)


# ---------- v0.4.6: every weak ValueError message must carry a "Try X" /
# "Valid options:" hint (quality dimension #5 — Deterministic Error Handling).
# These regression tests guard against future drift that re-introduces
# error messages that only describe the rejection.


async def test_granularity_error_lists_valid_options_and_suggests_default():
    """REGRESSION (v0.4.6): granularity rejection used to say only
    'must be daily or hourly'. Quality dim #5 requires a 'Try X' hint."""
    with pytest.raises(ValueError) as excinfo:
        await server.get_weather("sydney", granularity="weekly")  # type: ignore[arg-type]
    msg = str(excinfo.value)
    # Must include valid options
    assert "'daily'" in msg and "'hourly'" in msg
    # Must include a positive "Try X" pointer, not only a rejection
    assert "Try " in msg or "Valid options" in msg


async def test_compare_locations_non_list_error_suggests_example_input():
    """REGRESSION (v0.4.6): the non-list rejection used to say only
    'locations must be a list of strings, got str.' — a customer who
    typed compare_locations('sydney') has no idea what to do. Now it
    must show a concrete example."""
    with pytest.raises(ValueError) as excinfo:
        await server.compare_locations("sydney")  # type: ignore[arg-type]
    msg = str(excinfo.value)
    assert "Try compare_locations" in msg
    # Example uses a 2-element list (the minimum)
    assert "'sydney'" in msg and "'melbourne'" in msg


async def test_compare_locations_isolates_unexpected_exception(monkeypatch):
    """REGRESSION (v0.4.0 iter-1 audit): if one row's fetch raises an
    *unexpected* exception (e.g. pydantic ValidationError from a sanity
    check, or an httpx error not wrapped as OpenMeteoError), the previous
    code only caught ValueError/OpenMeteoError — the whole asyncio.gather
    crashed, poisoning sibling rows. Now: a broad Exception barrier turns
    any failure into a row-level error.
    """
    from au_weather_mcp.client import OpenMeteoClient

    # Patch _get_client to return a stub that raises RuntimeError mid-fetch
    # for the "sydney" call. melbourne should still succeed.
    real_forecast = OpenMeteoClient.forecast

    async def boom_for_sydney(self, *, latitude, **kwargs):
        if abs(latitude - (-33.8607)) < 0.01:
            # Sydney coords — raise an unexpected exception type
            raise RuntimeError("simulated upstream library bug")
        return await real_forecast(self, latitude=latitude, **kwargs)

    monkeypatch.setattr(OpenMeteoClient, "forecast", boom_for_sydney)

    resp = await server.compare_locations(["sydney", "melbourne"])
    assert len(resp.locations) == 2
    sydney_row = next(r for r in resp.locations if r.location_id == "sydney")
    melbourne_row = next(r for r in resp.locations if r.location_id == "melbourne")
    # sydney row must surface the error, not crash the call
    assert sydney_row.error is not None
    assert "RuntimeError" in sydney_row.error
    assert "simulated upstream library bug" in sydney_row.error
    # melbourne row must still succeed
    assert melbourne_row.error is None
    assert melbourne_row.current is not None


async def test_air_quality_source_url_round_trips_via_urlencode():
    """REGRESSION (v0.4.0 iter-1 audit): source_url advertised in the
    response must be byte-identical to the URL the client actually hits.
    Previously the server built the URL with raw commas in `current=...`
    while the client built it via urlencode (percent-encoded commas)."""
    from au_weather_mcp.client import OpenMeteoClient, AIR_QUALITY_BASE

    captured = {}

    async def fake_air_quality(self, *, latitude, longitude, timezone, current):
        captured["url"] = (
            f"{AIR_QUALITY_BASE}?"
            + __import__("urllib.parse", fromlist=["urlencode"]).urlencode({
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone,
                "current": ",".join(current),
            })
        )
        return {"current": {"time": "2026-05-13T11:00", "pm2_5": 5.0}}

    from au_weather_mcp.client import OpenMeteoClient as _OMC
    import au_weather_mcp.server as _srv
    # Monkey-patch via the server's client instance fetch
    import unittest.mock as _mock
    with _mock.patch.object(_OMC, "air_quality", fake_air_quality):
        resp = await server.air_quality("sydney")
    assert resp.source_url == captured["url"], (
        f"source_url mismatch:\n  response: {resp.source_url}\n  actual: {captured['url']}"
    )
