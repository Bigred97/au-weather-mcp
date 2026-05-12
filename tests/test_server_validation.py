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
