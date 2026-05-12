"""Curated location registry tests — schema, search, lookup."""
from __future__ import annotations

import pytest

from au_weather_mcp import curated


@pytest.fixture(autouse=True)
def reset_registry():
    curated.reset_registry()
    yield
    curated.reset_registry()


def test_list_ids_returns_21_locations():
    """The curated set is documented as 21 locations; this catches accidental
    YAML adds/removes that drift from the docs."""
    ids = curated.list_ids()
    assert len(ids) == 21, f"Expected 21 curated locations, got {len(ids)}: {ids}"


def test_all_capitals_present():
    ids = set(curated.list_ids())
    capitals = {"sydney", "melbourne", "brisbane", "perth", "adelaide",
                "hobart", "darwin", "canberra"}
    assert capitals <= ids, f"Missing capitals: {capitals - ids}"


def test_get_sydney_returns_full_metadata():
    syd = curated.get("sydney")
    assert syd is not None
    assert syd.name == "Sydney"
    assert syd.state == "NSW"
    assert syd.timezone == "Australia/Sydney"
    # Sydney lat/lng anchored to Observatory Hill; tight bounds catch
    # accidental edits.
    assert -34.0 <= syd.latitude <= -33.7
    assert 151.0 <= syd.longitude <= 151.3
    assert syd.nearest_bom_station and "066062" in syd.nearest_bom_station


def test_get_is_case_and_whitespace_insensitive():
    assert curated.get("SYDNEY") is curated.get("sydney")
    assert curated.get(" sydney ") is curated.get("sydney")
    assert curated.get("Sydney") is curated.get("sydney")


def test_get_unknown_returns_none():
    assert curated.get("totally_not_a_real_place") is None


def test_search_finds_capitals_by_name():
    results = curated.search("brisbane")
    assert results
    assert results[0].id == "brisbane"


def test_search_finds_by_state():
    """NSW search should surface NSW locations near the top."""
    results = curated.search("NSW", limit=5)
    ids = [r.id for r in results]
    nsw_locations = {"sydney", "newcastle", "wollongong"}
    matched = nsw_locations & set(ids)
    assert matched, f"Expected NSW locations in top 5, got {ids}"


def test_search_finds_regional_by_topic_phrase():
    """Descriptive phrases should match the description fields."""
    results = curated.search("tropical")
    ids = [r.id for r in results[:5]]
    # Cairns/Townsville/Darwin/Broome are the tropical ones
    assert any(i in ids for i in ("cairns", "townsville", "darwin", "broome"))


def test_all_locations_have_required_fields():
    """Every location must have lat/lng/timezone — no exceptions."""
    for loc in curated.all_locations():
        assert -45 <= loc.latitude <= -10, f"{loc.id} latitude {loc.latitude} outside AU"
        assert 110 <= loc.longitude <= 155, f"{loc.id} longitude {loc.longitude} outside AU"
        assert loc.timezone.startswith("Australia/"), (
            f"{loc.id} timezone {loc.timezone!r} not an Australia/ IANA name"
        )
        assert loc.name
        assert loc.state in ("NSW", "VIC", "QLD", "WA", "SA", "TAS", "NT", "ACT"), (
            f"{loc.id} state {loc.state!r} invalid"
        )


def test_all_location_ids_are_snake_case():
    """Catches accidental YAML edits that introduce dashes or capitals."""
    import re
    pattern = re.compile(r"^[a-z][a-z0-9_]*$")
    for loc_id in curated.list_ids():
        assert pattern.match(loc_id), f"Location id {loc_id!r} is not snake_case"
