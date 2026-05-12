"""Pydantic models for response envelopes.

Every response carries provenance fields (source_url, attribution,
retrieved_at, server_version) so an LLM agent can verify and cite the
data — and so a user can click through to Open-Meteo to confirm.

Sanity validators reject obviously bogus values (e.g. >55 °C in Australia,
RH outside 0-100) rather than silently passing through API hiccups.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Single source of truth for the attribution string. Used by build_response,
# describe_location, and the default on WeatherResponse — kept in one place
# so a change to the licensing wording can't go stale across surfaces.
DEFAULT_ATTRIBUTION = (
    "Weather data by Open-Meteo.com (https://open-meteo.com), licensed under "
    "CC BY 4.0. Underlying data includes the Australian Bureau of Meteorology "
    "(https://www.bom.gov.au) under Open-Meteo's licensing arrangement."
)
OSM_ATTRIBUTION_SUFFIX = (
    " Postcode geocoding by © OpenStreetMap contributors, licensed under "
    "the Open Database Licence (ODbL) — https://www.openstreetmap.org/copyright."
)

# Every value `location_resolution` can legitimately take. Adding a new
# resolution path requires updating this Literal AND the resolution.py
# stage list — Pydantic will reject any unlisted value, catching the
# 'forgot to update the type' mistake at write time.
LocationResolution = Literal[
    "curated",
    "state_alias",
    "raw_coordinates",
    "postcode",
    "geocoded",
    "fuzzy_curated",
]

# Australian temperature extremes (BOM-recorded all-time):
#   Hottest: 50.7 °C, Onslow WA, 13 Jan 2022 (also Oodnadatta 50.7 °C 1960)
#   Coldest: −23.0 °C, Charlotte Pass NSW, 29 Jun 1994
# We use slightly wider bounds to allow future extremes + station-level noise.
AU_TEMP_MIN_C = -30.0
AU_TEMP_MAX_C = 55.0
# Pressure: Australian sea-level pressures sit ~975-1040 hPa typically.
# Cyclones bottom out around 920 hPa (Cyclone Mahina, 1899 — 880 hPa unofficial).
# Wider bounds for safety.
AU_PRESSURE_MIN_HPA = 850.0
AU_PRESSURE_MAX_HPA = 1080.0


class LocationSummary(BaseModel):
    """A curated AU location — surface for search_locations and list_curated."""
    id: str
    name: str
    state: str  # NSW, VIC, etc.
    description: str | None = None


class LocationDetail(BaseModel):
    """Full metadata for one location — surface for describe_location.

    `id` is the curated key when the input resolved against the curated YAML
    (e.g. 'sydney', 'gold_coast'); None when the input was resolved via
    postcode lookup, raw coordinates, or Open-Meteo geocoding. Matching
    `WeatherResponse.location_id` semantics.
    """
    id: str | None
    name: str
    state: str
    description: str | None = None
    latitude: float
    longitude: float
    timezone: str
    elevation_m: float | None = None
    nearest_bom_station: str | None = None
    open_meteo_url: str  # the exact URL Open-Meteo serves data for this location
    attribution: str


class WeatherObservation(BaseModel):
    """A single point-in-time weather record.

    `value` semantics: SI / metric units throughout (Celsius, mm, km/h, hPa, %).
    `time` is local to the location's timezone, ISO-8601 format.
    None on a field means the upstream source reported missing — not zero.
    """
    time: str  # ISO-8601 local time, e.g. "2026-05-12T11:30"
    temperature_c: float | None = None
    apparent_temperature_c: float | None = None
    relative_humidity_pct: int | None = None
    precipitation_mm: float | None = None
    rain_mm: float | None = None
    cloud_cover_pct: int | None = None
    pressure_msl_hpa: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: int | None = None
    wind_gusts_kmh: float | None = None
    weather_code: int | None = None  # WMO code (0=clear, 1=mostly clear, ..., 95=thunderstorm)
    weather_description: str | None = None  # plain-English from weather_code

    @field_validator("temperature_c", "apparent_temperature_c")
    @classmethod
    def _temp_sanity(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not AU_TEMP_MIN_C <= v <= AU_TEMP_MAX_C:
            raise ValueError(
                f"temperature {v} °C outside plausible Australian range "
                f"[{AU_TEMP_MIN_C}, {AU_TEMP_MAX_C}]"
            )
        return v

    @field_validator("relative_humidity_pct", "cloud_cover_pct")
    @classmethod
    def _pct_sanity(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not 0 <= v <= 100:
            raise ValueError(f"percentage {v} outside 0-100")
        return v

    @field_validator("pressure_msl_hpa")
    @classmethod
    def _pressure_sanity(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not AU_PRESSURE_MIN_HPA <= v <= AU_PRESSURE_MAX_HPA:
            raise ValueError(
                f"pressure {v} hPa outside plausible range "
                f"[{AU_PRESSURE_MIN_HPA}, {AU_PRESSURE_MAX_HPA}]"
            )
        return v

    @field_validator("wind_direction_deg")
    @classmethod
    def _wind_dir_sanity(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not 0 <= v <= 360:
            raise ValueError(f"wind_direction {v}° outside 0-360")
        return v


class DailyAggregate(BaseModel):
    """A daily-aggregated weather record."""
    date: str  # YYYY-MM-DD
    temperature_max_c: float | None = None
    temperature_min_c: float | None = None
    apparent_temperature_max_c: float | None = None
    apparent_temperature_min_c: float | None = None
    precipitation_sum_mm: float | None = None
    rain_sum_mm: float | None = None
    wind_speed_max_kmh: float | None = None
    wind_gusts_max_kmh: float | None = None
    weather_code: int | None = None
    weather_description: str | None = None
    sunshine_duration_s: float | None = None

    @field_validator("temperature_max_c", "temperature_min_c",
                     "apparent_temperature_max_c", "apparent_temperature_min_c")
    @classmethod
    def _temp_sanity(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not AU_TEMP_MIN_C <= v <= AU_TEMP_MAX_C:
            raise ValueError(
                f"daily temperature {v} °C outside plausible AU range"
            )
        return v


class WeatherResponse(BaseModel):
    """Common response envelope for current/forecast/historical queries.

    Trust contract: every field below is populated on every successful
    response. Stale-cache responses set `stale: True` with a reason.

    `location_id` is the curated key when the input resolved to a curated
    location, otherwise None (e.g. for raw coordinates or geocoded place
    names). `location_resolution` explains how we got here — see
    resolution.ResolvedLocation.source values.
    """
    location_id: str | None
    location_name: str
    state: str | None
    latitude: float
    longitude: float
    timezone: str
    location_resolution: LocationResolution
    location_input: str  # what the user actually typed
    query: dict[str, Any] = Field(default_factory=dict)
    period: dict[str, str | None] = Field(default_factory=lambda: {"start": None, "end": None})
    # Exactly one of `current` / `hourly` / `daily` is populated per response,
    # depending on which kind of query the caller asked for.
    current: WeatherObservation | None = None
    hourly: list[WeatherObservation] | None = None
    daily: list[DailyAggregate] | None = None
    source: str = "Open-Meteo (aggregates Bureau of Meteorology data under licence)"
    attribution: str = DEFAULT_ATTRIBUTION
    source_url: str  # the exact Open-Meteo URL this response came from
    retrieved_at: datetime
    server_version: str
    stale: bool = False
    stale_reason: str | None = None


# ---------- v0.4.0: air quality ----------

class AirQualityObservation(BaseModel):
    """Current air-quality reading. Concentrations in µg/m³; AQI scales unitless.

    European AQI: 0-20 good, 20-40 fair, 40-60 moderate, 60-80 poor,
    80-100 very poor, >100 extremely poor.
    US AQI: 0-50 good, 51-100 moderate, 101-150 unhealthy for sensitive,
    151-200 unhealthy, 201-300 very unhealthy, >300 hazardous.
    """
    time: str
    pm2_5_ugm3: float | None = None
    pm10_ugm3: float | None = None
    ozone_ugm3: float | None = None
    nitrogen_dioxide_ugm3: float | None = None
    sulphur_dioxide_ugm3: float | None = None
    carbon_monoxide_ugm3: float | None = None
    european_aqi: int | None = None
    us_aqi: int | None = None
    european_aqi_label: str | None = None  # plain-English label derived from EU AQI
    us_aqi_label: str | None = None  # plain-English label derived from US AQI


class AirQualityResponse(BaseModel):
    """Air-quality response envelope. Same trust contract as WeatherResponse:
    source_url, attribution, retrieved_at, server_version, location_resolution."""
    location_id: str | None
    location_name: str
    state: str | None
    latitude: float
    longitude: float
    timezone: str
    location_resolution: LocationResolution
    location_input: str
    current: AirQualityObservation | None = None
    source: str = "Open-Meteo Air Quality (CAMS European + global merge)"
    attribution: str = DEFAULT_ATTRIBUTION
    source_url: str
    retrieved_at: datetime
    server_version: str
    stale: bool = False
    stale_reason: str | None = None


# ---------- v0.4.0: multi-location comparison ----------

class ComparisonRow(BaseModel):
    """One location's slice of a multi-location comparison."""
    location_id: str | None
    location_name: str
    state: str | None
    latitude: float
    longitude: float
    timezone: str
    location_resolution: LocationResolution
    location_input: str
    current: WeatherObservation | None = None
    source_url: str
    error: str | None = None  # populated if this location failed to resolve / fetch


class ComparisonResponse(BaseModel):
    """Side-by-side weather comparison across N locations. Each ComparisonRow
    has the same shape so agents can iterate cleanly. Failed rows surface
    an `error` field rather than crashing the whole call."""
    metric: str  # 'current' is the only metric in v0.4.0; reserved for future
    locations: list[ComparisonRow]
    retrieved_at: datetime
    server_version: str
    source: str = "Open-Meteo (aggregates Bureau of Meteorology data under licence)"
    attribution: str = DEFAULT_ATTRIBUTION
