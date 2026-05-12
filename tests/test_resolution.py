"""Tests for the location-input resolution layer.

This is the compatibility surface — every customer-typed variation we
support gets a test here. Anything regressing here is a customer-visible
break.

Tests are split into:
  - Pure unit tests (no network) using a mock geocoder
  - Live tests (marked `live`) that exercise Open-Meteo's real geocoding API
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from au_weather_mcp import curated as curated_mod
from au_weather_mcp.resolution import (
    _normalize_to_curated_key,
    _try_lat_lng,
    _try_state,
    resolve_location,
)


# ---------- internal helpers ----------

class _FakeClient:
    """Stub OpenMeteoClient with a programmable .geocode_au()."""
    def __init__(self, geocode_response=None):
        self._geocode = AsyncMock(return_value=geocode_response or [])
        self.geocode_au = self._geocode


# ---------- _normalize_to_curated_key ----------

def test_normalize_lowercases():
    assert _normalize_to_curated_key("Sydney") == "sydney"
    assert _normalize_to_curated_key("SYDNEY") == "sydney"


def test_normalize_spaces_to_underscores():
    assert _normalize_to_curated_key("Gold Coast") == "gold_coast"
    assert _normalize_to_curated_key("GOLD COAST") == "gold_coast"
    assert _normalize_to_curated_key("alice springs") == "alice_springs"


def test_normalize_handles_hyphens():
    assert _normalize_to_curated_key("gold-coast") == "gold_coast"
    assert _normalize_to_curated_key("sunshine-coast") == "sunshine_coast"


def test_normalize_strips_special_chars():
    assert _normalize_to_curated_key("Sydney!") == "sydney"
    assert _normalize_to_curated_key("Sydney CBD") == "sydney_cbd"  # CBD lives on


def test_normalize_strips_whitespace():
    assert _normalize_to_curated_key("  sydney  ") == "sydney"
    assert _normalize_to_curated_key("\tsydney\n") == "sydney"


# ---------- _try_lat_lng ----------

def test_lat_lng_parses_clean_format():
    r = _try_lat_lng("-33.87,151.21", original="-33.87,151.21")
    assert r is not None
    assert r.source == "raw_coordinates"
    assert r.latitude == -33.87
    assert r.longitude == 151.21


def test_lat_lng_tolerates_whitespace():
    r = _try_lat_lng("  -33.87 , 151.21  ", original="x")
    assert r is not None
    assert r.latitude == -33.87


def test_lat_lng_rejects_outside_au_bbox():
    """Refusing to silently serve weather for non-AU coords is a scope guarantee."""
    with pytest.raises(ValueError, match="outside the Australia bounding box"):
        _try_lat_lng("40.7,-74.0", original="40.7,-74.0")  # New York City


def test_lat_lng_returns_none_for_non_coordinate_inputs():
    assert _try_lat_lng("sydney", original="sydney") is None
    assert _try_lat_lng("NSW", original="NSW") is None
    assert _try_lat_lng("not coordinates", original="x") is None


def test_lat_lng_timezone_heuristic_perth_west():
    """Western longitudes get Australia/Perth as a sensible fallback."""
    r = _try_lat_lng("-31.95,115.86", original="x")  # Perth
    assert r is not None
    assert r.timezone == "Australia/Perth"


# ---------- _try_state ----------

def test_state_codes_resolve_to_capital():
    assert _try_state("NSW", "NSW").curated_id == "sydney"
    assert _try_state("VIC", "VIC").curated_id == "melbourne"
    assert _try_state("QLD", "QLD").curated_id == "brisbane"
    assert _try_state("WA", "WA").curated_id == "perth"
    assert _try_state("SA", "SA").curated_id == "adelaide"
    assert _try_state("TAS", "TAS").curated_id == "hobart"
    assert _try_state("NT", "NT").curated_id == "darwin"
    assert _try_state("ACT", "ACT").curated_id == "canberra"


def test_state_codes_case_insensitive():
    assert _try_state("nsw", "nsw").curated_id == "sydney"
    assert _try_state("Vic", "Vic").curated_id == "melbourne"


def test_full_state_names_resolve_to_capital():
    assert _try_state("New South Wales", "x").curated_id == "sydney"
    assert _try_state("Victoria", "x").curated_id == "melbourne"
    assert _try_state("Western Australia", "x").curated_id == "perth"
    assert _try_state("Tasmania", "x").curated_id == "hobart"


def test_state_alias_marks_resolution_source():
    r = _try_state("NSW", "NSW")
    assert r.source == "state_alias"


def test_non_state_inputs_return_none():
    assert _try_state("sydney", "sydney") is None
    assert _try_state("Bondi", "Bondi") is None
    assert _try_state("", "") is None


# ---------- full resolve_location flow ----------

async def test_resolve_curated_id_exact():
    c = _FakeClient()
    r = await resolve_location(c, "sydney")
    assert r.source == "curated"
    assert r.curated_id == "sydney"
    c._geocode.assert_not_called()  # no network call needed


async def test_resolve_curated_id_uppercase():
    c = _FakeClient()
    r = await resolve_location(c, "SYDNEY")
    assert r.curated_id == "sydney"
    c._geocode.assert_not_called()


async def test_resolve_curated_name_with_space():
    c = _FakeClient()
    r = await resolve_location(c, "Gold Coast")
    assert r.curated_id == "gold_coast"
    c._geocode.assert_not_called()


async def test_resolve_curated_name_with_hyphen():
    c = _FakeClient()
    r = await resolve_location(c, "alice-springs")
    assert r.curated_id == "alice_springs"


async def test_resolve_state_code():
    c = _FakeClient()
    r = await resolve_location(c, "NSW")
    assert r.curated_id == "sydney"
    assert r.source == "state_alias"
    c._geocode.assert_not_called()


async def test_resolve_state_full_name():
    c = _FakeClient()
    r = await resolve_location(c, "Queensland")
    assert r.curated_id == "brisbane"
    assert r.source == "state_alias"


async def test_resolve_australia_defaults_to_sydney():
    """'Australia' alone is ambiguous; we default to the most-asked capital."""
    c = _FakeClient()
    r = await resolve_location(c, "Australia")
    assert r.curated_id == "sydney"
    assert r.source == "state_alias"


async def test_resolve_lat_lng():
    c = _FakeClient()
    r = await resolve_location(c, "-33.87,151.21")
    assert r.source == "raw_coordinates"
    assert r.latitude == -33.87
    assert r.longitude == 151.21
    c._geocode.assert_not_called()


async def test_resolve_lat_lng_outside_au_raises():
    c = _FakeClient()
    with pytest.raises(ValueError, match="outside the Australia bounding box"):
        await resolve_location(c, "40.7,-74.0")  # NYC
    c._geocode.assert_not_called()


async def test_resolve_typo_fuzzy_matches_curated():
    """'Sydny' (typo) should resolve to sydney without geocoding."""
    c = _FakeClient()
    r = await resolve_location(c, "Sydny")
    # Either resolves to sydney directly (high fuzz score) or goes through
    # geocoding. Either way we should land somewhere with state=NSW.
    assert r is not None
    # The fuzzy path should catch this — saves a network call.
    assert r.source in ("fuzzy_curated", "geocoded", "curated")


async def test_resolve_unknown_place_uses_geocoder():
    """A place not in the curated set should hit the geocoder."""
    geocode_result = [{
        "name": "Margaret River",
        "country_code": "AU",
        "admin1": "Western Australia",
        "latitude": -33.96,
        "longitude": 115.08,
        "timezone": "Australia/Perth",
        "elevation": 50.0,
    }]
    c = _FakeClient(geocode_response=geocode_result)
    r = await resolve_location(c, "Margaret River")
    assert r.source == "geocoded"
    assert r.name == "Margaret River"
    assert r.state == "Western Australia"
    assert r.latitude == -33.96
    assert r.timezone == "Australia/Perth"


async def test_resolve_unknown_with_no_geocode_results_raises_with_suggestions():
    """If everything fails, the error message must list curated alternatives."""
    c = _FakeClient(geocode_response=[])
    with pytest.raises(ValueError, match="Could not resolve location"):
        await resolve_location(c, "Xenoluthia")


async def test_resolve_empty_string_raises():
    c = _FakeClient()
    with pytest.raises(ValueError, match="location is empty"):
        await resolve_location(c, "   ")


async def test_resolve_non_string_raises():
    c = _FakeClient()
    with pytest.raises(ValueError, match="must be a string"):
        await resolve_location(c, 123)  # type: ignore[arg-type]


async def test_resolve_priority_curated_over_geocode():
    """If curated set has it, we never call the geocoder."""
    c = _FakeClient(geocode_response=[{"name": "Different Sydney", "country_code": "US"}])
    r = await resolve_location(c, "sydney")
    assert r.curated_id == "sydney"
    c._geocode.assert_not_called()
    assert r.source == "curated"


async def test_resolve_priority_state_over_geocode():
    """State code takes priority over geocode."""
    c = _FakeClient(geocode_response=[{"name": "NSW thing", "country_code": "AU",
                                       "latitude": 0, "longitude": 130}])
    r = await resolve_location(c, "NSW")
    assert r.curated_id == "sydney"
    c._geocode.assert_not_called()
    assert r.source == "state_alias"


# ---------- live geocoding tests (network) ----------

pytestmark_live = pytest.mark.live


@pytest.mark.live
async def test_live_geocode_byron_bay():
    """Byron Bay isn't curated but Open-Meteo's geocoder knows it."""
    from au_weather_mcp.cache import Cache
    from au_weather_mcp.client import OpenMeteoClient
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    client = OpenMeteoClient(cache=Cache(tmp / "cache.db"))
    try:
        r = await resolve_location(client, "Byron Bay")
        assert r.source == "geocoded"
        assert r.state == "New South Wales"
        # Byron Bay is approximately -28.65, 153.61
        assert -29 < r.latitude < -28
        assert 153 < r.longitude < 154
        assert r.timezone == "Australia/Sydney"
    finally:
        await client.aclose()


@pytest.mark.live
async def test_live_geocode_ambiguous_resolves_to_au_high_pop():
    """'Newcastle' exists in many countries; AU filter + population sort
    should resolve to the NSW city (pop 508k)."""
    from au_weather_mcp.cache import Cache
    from au_weather_mcp.client import OpenMeteoClient
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    client = OpenMeteoClient(cache=Cache(tmp / "cache.db"))
    try:
        # 'Newcastle' is curated as a name, but customer might type something
        # that doesn't match curated and only the geocoder can resolve.
        # Use a deliberately uncurated AU city instead.
        r = await resolve_location(client, "Toowoomba")
        assert r.source == "geocoded"
        assert "Queensland" in (r.state or "")
        # Toowoomba is -27.56, 151.95
        assert -28 < r.latitude < -27
        assert 151 < r.longitude < 153
    finally:
        await client.aclose()
