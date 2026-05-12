"""Shaping tests — pivot Open-Meteo column-arrays to row-objects, validate
sanity bounds, and verify the response envelope always carries provenance."""
from __future__ import annotations

import pytest

from au_weather_mcp.models import WeatherObservation
from au_weather_mcp.resolution import ResolvedLocation
from au_weather_mcp.shaping import (
    _current_from_payload,
    _daily_from_payload,
    _hourly_from_payload,
    build_response,
    describe_weather_code,
)


@pytest.fixture
def sydney():
    return ResolvedLocation(
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


def test_describe_weather_code_known():
    assert describe_weather_code(0) == "Clear sky"
    assert describe_weather_code(95) == "Thunderstorm"
    assert describe_weather_code(3) == "Overcast"


def test_describe_weather_code_unknown_returns_none():
    """Unknown codes return None — agent should treat as 'no description'
    rather than getting a misleading 'Unknown' string."""
    assert describe_weather_code(9999) is None


def test_describe_weather_code_none_input():
    assert describe_weather_code(None) is None


def test_current_pivot_full_payload():
    obs = _current_from_payload({
        "time": "2026-05-12T11:30",
        "temperature_2m": 19.7,
        "apparent_temperature": 18.1,
        "relative_humidity_2m": 67,
        "precipitation": 0.0,
        "rain": 0.0,
        "cloud_cover": 43,
        "pressure_msl": 1034.5,
        "wind_speed_10m": 18.4,
        "wind_direction_10m": 149,
        "wind_gusts_10m": 43.2,
        "weather_code": 1,
    })
    assert obs.temperature_c == 19.7
    assert obs.weather_description == "Mainly clear"


def test_hourly_pivot_column_to_rows():
    rows = _hourly_from_payload({
        "time": ["2026-05-12T00:00", "2026-05-12T01:00", "2026-05-12T02:00"],
        "temperature_2m": [14.2, 13.9, 13.5],
        "relative_humidity_2m": [80, 82, 85],
        "weather_code": [1, 2, 3],
    })
    assert len(rows) == 3
    assert rows[0].temperature_c == 14.2
    assert rows[2].weather_description == "Overcast"


def test_hourly_pivot_handles_missing_columns():
    """If Open-Meteo doesn't return a requested variable (e.g. agreed wind not
    asked for), our pivot should still produce the rows with None for that
    field rather than crashing."""
    rows = _hourly_from_payload({
        "time": ["2026-05-12T00:00"],
        "temperature_2m": [14.2],
        # no wind, no humidity
    })
    assert len(rows) == 1
    assert rows[0].temperature_c == 14.2
    assert rows[0].wind_speed_kmh is None
    assert rows[0].relative_humidity_pct is None


def test_daily_pivot():
    rows = _daily_from_payload({
        "time": ["2020-01-01", "2020-01-02"],
        "temperature_2m_max": [24.1, 25.4],
        "temperature_2m_min": [19.4, 20.4],
        "precipitation_sum": [0.0, 0.9],
        "weather_code": [1, 61],
    })
    assert len(rows) == 2
    assert rows[0].date == "2020-01-01"
    assert rows[1].temperature_max_c == 25.4
    assert rows[1].weather_description == "Slight rain"


def test_observation_rejects_implausible_temperature():
    """The sanity validator must reject obviously bogus values rather than
    silently passing them through to the agent."""
    with pytest.raises(Exception, match="outside plausible Australian range"):
        WeatherObservation(time="2024-01-01T00:00", temperature_c=200.0)
    with pytest.raises(Exception, match="outside plausible Australian range"):
        WeatherObservation(time="2024-01-01T00:00", temperature_c=-100.0)


def test_observation_rejects_implausible_humidity():
    with pytest.raises(Exception, match="0-100"):
        WeatherObservation(time="2024-01-01T00:00", relative_humidity_pct=150)
    with pytest.raises(Exception, match="0-100"):
        WeatherObservation(time="2024-01-01T00:00", relative_humidity_pct=-10)


def test_observation_rejects_implausible_pressure():
    with pytest.raises(Exception, match="outside plausible range"):
        WeatherObservation(time="2024-01-01T00:00", pressure_msl_hpa=500)


def test_observation_rejects_invalid_wind_direction():
    with pytest.raises(Exception, match="0-360"):
        WeatherObservation(time="2024-01-01T00:00", wind_direction_deg=999)


def test_observation_accepts_extreme_but_real_values():
    """The validators must NOT reject real recorded Australian extremes."""
    # Onslow WA, 13 Jan 2022 — official BOM record
    WeatherObservation(time="2022-01-13T15:00", temperature_c=50.7)
    # Charlotte Pass NSW, 29 Jun 1994 — official BOM record
    WeatherObservation(time="1994-06-29T07:00", temperature_c=-23.0)


def test_build_response_envelope_includes_provenance(sydney):
    """Trust contract: every successful response carries source_url,
    attribution, retrieved_at, and server_version."""
    payload = {
        "current": {
            "time": "2026-05-12T11:30",
            "temperature_2m": 19.7,
            "weather_code": 1,
        }
    }
    resp = build_response(
        location=sydney,
        payload=payload,
        source_url="https://api.open-meteo.com/v1/forecast?example=1",
        user_query={"location": "sydney"},
    )
    assert resp.source_url == "https://api.open-meteo.com/v1/forecast?example=1"
    assert "CC BY 4.0" in resp.attribution
    assert resp.retrieved_at is not None
    assert resp.server_version  # any non-empty string from importlib.metadata
    assert resp.stale is False
    assert resp.current is not None
    assert resp.current.temperature_c == 19.7
