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

from au_weather_mcp.resolution import (
    _normalize_to_curated_key,
    _try_lat_lng,
    _try_state,
    resolve_location,
)


# ---------- internal helpers ----------

class _FakeClient:
    """Stub OpenMeteoClient with programmable .geocode_au() and
    .geocode_postcode_au()."""
    def __init__(self, geocode_response=None, postcode_response=None):
        self._geocode = AsyncMock(return_value=geocode_response or [])
        self._postcode = AsyncMock(return_value=postcode_response)
        self.geocode_au = self._geocode
        self.geocode_postcode_au = self._postcode


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


async def test_resolve_underscore_form_normalised_for_geocoder():
    """Regression: an underscore place-name not in the curated set must
    normalise to spaces before hitting the Open-Meteo geocoder — otherwise
    'margaret_river' returns no candidates while 'Margaret River' works.
    Both shapes are publicly documented as accepted."""
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
    r = await resolve_location(c, "margaret_river")
    assert r.source == "geocoded"
    assert r.name == "Margaret River"
    # The query passed to the geocoder must be the spaced form
    c._geocode.assert_called_once()
    call_args = c._geocode.call_args
    geocoder_query = call_args.args[0] if call_args.args else call_args.kwargs.get("name")
    assert "_" not in geocoder_query, (
        f"Expected underscores normalised to spaces before geocoding, "
        f"got {geocoder_query!r}"
    )
    assert geocoder_query.strip().lower() == "margaret river"


async def test_resolve_underscore_and_spaced_forms_equivalent():
    """Regression: 'byron_bay' and 'Byron Bay' must resolve to the same
    ResolvedLocation. Earlier versions broke this when the underscore form
    was passed through unchanged to a geocoder that doesn't understand it."""
    geocode_result = [{
        "name": "Byron Bay",
        "country_code": "AU",
        "admin1": "New South Wales",
        "latitude": -28.65,
        "longitude": 153.61,
        "timezone": "Australia/Sydney",
        "elevation": 5.0,
    }]
    c_under = _FakeClient(geocode_response=geocode_result)
    r_under = await resolve_location(c_under, "byron_bay")
    c_space = _FakeClient(geocode_response=geocode_result)
    r_space = await resolve_location(c_space, "Byron Bay")
    assert r_under.latitude == r_space.latitude
    assert r_under.longitude == r_space.longitude
    assert r_under.name == r_space.name
    assert r_under.state == r_space.state
    assert r_under.source == r_space.source == "geocoded"


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


# ---------- postcode resolution ----------

async def test_resolve_postcode_via_nominatim():
    """4-digit postcode should route to Nominatim, not Open-Meteo geocoder."""
    nominatim_response = {
        "lat": "-33.8884334",
        "lon": "151.2700666",
        "display_name": "2026, Bondi Beach, Waverley Council, New South Wales, 2026, Australia",
        "address": {
            "suburb": "Bondi Beach",
            "state": "New South Wales",
        },
    }
    c = _FakeClient(postcode_response=nominatim_response)
    r = await resolve_location(c, "2026")
    assert r.source == "postcode"
    assert r.name == "Bondi Beach"
    assert r.state == "New South Wales"
    assert abs(r.latitude - -33.8884334) < 0.001
    assert r.timezone == "Australia/Sydney"  # 151°E → NSW/VIC/QLD/TAS zone
    c._postcode.assert_called_once_with("2026")
    # Postcode took priority — no Open-Meteo geocode call
    c._geocode.assert_not_called()


async def test_resolve_postcode_no_address_falls_back_to_display_name():
    """Some Nominatim responses don't have address.suburb but display_name
    always works."""
    nominatim_response = {
        "lat": "-27.466",
        "lon": "153.024",
        "display_name": "4000, Brisbane City, Brisbane, Queensland, Australia",
        # no `address` dict
    }
    c = _FakeClient(postcode_response=nominatim_response)
    r = await resolve_location(c, "4000")
    assert r.source == "postcode"
    assert r.name == "Brisbane City"  # parsed from display_name
    assert r.state == "Queensland"
    assert r.timezone == "Australia/Brisbane"


async def test_resolve_postcode_nominatim_returns_nothing_falls_through():
    """If Nominatim has no result for a 4-digit numeric, we should fall
    through to the Open-Meteo geocoder rather than erroring."""
    om_response = [{
        "name": "Some Place", "country_code": "AU", "admin1": "NSW",
        "latitude": -33.0, "longitude": 150.0, "timezone": "Australia/Sydney",
    }]
    c = _FakeClient(postcode_response=None, geocode_response=om_response)
    r = await resolve_location(c, "9999")
    # Fell through to Open-Meteo
    assert r.source == "geocoded"
    c._postcode.assert_called_once()
    c._geocode.assert_called_once()


async def test_resolve_non_postcode_skips_nominatim():
    """A 3-digit or 5-digit numeric is not a postcode — skip Nominatim."""
    c = _FakeClient()
    with pytest.raises(ValueError):
        # Will raise because no resolution path matches
        await resolve_location(c, "12345")
    c._postcode.assert_not_called()


async def test_resolve_postcode_unparseable_lat_returns_none():
    """If Nominatim returns garbage lat/lng, postcode stage returns None and
    we fall through to Open-Meteo."""
    bad_response = {"lat": "not_a_number", "lon": "0", "display_name": "x"}
    om_response = [{
        "name": "Fallback", "country_code": "AU",
        "latitude": -34.0, "longitude": 151.0, "timezone": "Australia/Sydney",
    }]
    c = _FakeClient(postcode_response=bad_response, geocode_response=om_response)
    r = await resolve_location(c, "2026")
    # Should have fallen through to Open-Meteo
    assert r.source == "geocoded"


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
async def test_live_postcode_2026_resolves_to_bondi_beach():
    """End-to-end Nominatim test for the canonical Bondi Beach postcode."""
    from au_weather_mcp.cache import Cache
    from au_weather_mcp.client import OpenMeteoClient
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    client = OpenMeteoClient(cache=Cache(tmp / "cache.db"))
    try:
        r = await resolve_location(client, "2026")
        assert r.source == "postcode"
        # Bondi Beach is approximately -33.89, 151.27
        assert -34.0 < r.latitude < -33.8
        assert 151.2 < r.longitude < 151.3
        assert r.state == "New South Wales"
        assert "Bondi" in r.name
        assert r.timezone == "Australia/Sydney"
    finally:
        await client.aclose()


def test_postcode_attribution_includes_osm():
    """When the location resolves via postcode, OSM attribution must appear
    in the response envelope — required by Nominatim's ODbL licence.

    Unit test (not live) — exercises shaping.build_response with a
    postcode-source ResolvedLocation directly. The live equivalent runs
    via the test_live_postcode_2026_resolves_to_bondi_beach test above."""
    from au_weather_mcp.resolution import ResolvedLocation
    from au_weather_mcp.shaping import build_response
    bondi = ResolvedLocation(
        name="Bondi Beach",
        state="New South Wales",
        country="Australia",
        latitude=-33.8884,
        longitude=151.2701,
        timezone="Australia/Sydney",
        elevation_m=None,
        curated_id=None,
        source="postcode",
        original_input="2026",
    )
    payload = {"current": {"time": "2026-05-12T11:00", "temperature_2m": 19.0}}
    resp = build_response(
        location=bondi,
        payload=payload,
        source_url="https://api.open-meteo.com/v1/forecast?example=1",
        user_query={"location": "2026"},
    )
    assert resp.location_resolution == "postcode"
    assert "OpenStreetMap" in resp.attribution
    assert "ODbL" in resp.attribution
    # The base Open-Meteo + BOM attribution must still be present alongside
    assert "Open-Meteo" in resp.attribution
    assert "Bureau of Meteorology" in resp.attribution


def test_non_postcode_attribution_omits_osm():
    """Curated responses must NOT include OSM attribution — we didn't use OSM."""
    from au_weather_mcp.resolution import ResolvedLocation
    from au_weather_mcp.shaping import build_response
    syd = ResolvedLocation(
        name="Sydney",
        state="NSW",
        country="Australia",
        latitude=-33.86,
        longitude=151.21,
        timezone="Australia/Sydney",
        elevation_m=39,
        curated_id="sydney",
        source="curated",
        original_input="sydney",
    )
    payload = {"current": {"time": "2026-05-12T11:00", "temperature_2m": 19.0}}
    resp = build_response(
        location=syd, payload=payload,
        source_url="https://example.com",
        user_query={"location": "sydney"},
    )
    assert "OpenStreetMap" not in resp.attribution
    assert "ODbL" not in resp.attribution


@pytest.mark.live
async def test_live_geocode_ambiguous_resolves_to_au_high_pop():
    """'Surfers Paradise' isn't in the curated set; AU filter + population
    sort on the geocoder should resolve to the QLD locality."""
    from au_weather_mcp.cache import Cache
    from au_weather_mcp.client import OpenMeteoClient
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    client = OpenMeteoClient(cache=Cache(tmp / "cache.db"))
    try:
        # Need a place not in the curated set so the geocoder is actually
        # invoked. Surfers Paradise is on the Gold Coast — close to curated
        # gold_coast but not the same id, and not normalisable.
        r = await resolve_location(client, "Surfers Paradise")
        assert r.source == "geocoded"
        # Surfers Paradise is around -28.0, 153.4
        assert -28.5 < r.latitude < -27.5
        assert 153 < r.longitude < 154
    finally:
        await client.aclose()
