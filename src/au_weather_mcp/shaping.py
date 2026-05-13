"""Translate raw Open-Meteo JSON into our typed WeatherResponse envelope.

Open-Meteo's JSON has the shape `{ current: {temperature_2m: 19.7, ...},
hourly: {time: [...], temperature_2m: [...]}, daily: {...} }` — column-oriented
arrays. We pivot to row-oriented objects (one WeatherObservation per time
step) because that's what an LLM agent expects to iterate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import __version__
from .models import (
    DEFAULT_ATTRIBUTION,
    OSM_ATTRIBUTION_SUFFIX,
    AirQualityObservation,
    AirQualityResponse,
    DailyAggregate,
    WeatherObservation,
    WeatherResponse,
)
from .resolution import ResolvedLocation

# WMO weather codes: https://open-meteo.com/en/docs (and WMO 4677 standard).
# Mapping comes from Open-Meteo's documentation page. Used to surface a
# plain-English description alongside the raw integer code.
WMO_WEATHER_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def describe_weather_code(code: int | None) -> str | None:
    """Resolve a WMO weather code to plain English. Unknown codes return None
    rather than 'Unknown' — the agent should treat None as 'no description
    available' and ignore that field."""
    if code is None:
        return None
    return WMO_WEATHER_CODES.get(code)


def _current_from_payload(current: dict[str, Any]) -> WeatherObservation:
    """Open-Meteo's `current` block is a flat dict — pivot it to our model."""
    code = current.get("weather_code")
    return WeatherObservation(
        time=str(current.get("time", "")),
        temperature_c=current.get("temperature_2m"),
        apparent_temperature_c=current.get("apparent_temperature"),
        relative_humidity_pct=current.get("relative_humidity_2m"),
        precipitation_mm=current.get("precipitation"),
        rain_mm=current.get("rain"),
        cloud_cover_pct=current.get("cloud_cover"),
        pressure_msl_hpa=current.get("pressure_msl"),
        wind_speed_kmh=current.get("wind_speed_10m"),
        wind_direction_deg=current.get("wind_direction_10m"),
        wind_gusts_kmh=current.get("wind_gusts_10m"),
        weather_code=code,
        weather_description=describe_weather_code(code),
    )


def _hourly_from_payload(hourly: dict[str, Any]) -> list[WeatherObservation]:
    """Open-Meteo `hourly` is column-arrays: pivot to a list of records.

    Missing fields are silently absent from the column dict — we default each
    requested column to an empty list so .get returns None safely.
    """
    times: list[str] = hourly.get("time") or []
    n = len(times)

    def col(name: str) -> list[Any]:
        return hourly.get(name) or [None] * n

    temp = col("temperature_2m")
    app_temp = col("apparent_temperature")
    rh = col("relative_humidity_2m")
    precip = col("precipitation")
    rain = col("rain")
    cloud = col("cloud_cover")
    press = col("pressure_msl")
    wind_s = col("wind_speed_10m")
    wind_d = col("wind_direction_10m")
    wind_g = col("wind_gusts_10m")
    code = col("weather_code")

    out: list[WeatherObservation] = []
    for i in range(n):
        out.append(
            WeatherObservation(
                time=times[i],
                temperature_c=temp[i],
                apparent_temperature_c=app_temp[i],
                relative_humidity_pct=rh[i],
                precipitation_mm=precip[i],
                rain_mm=rain[i],
                cloud_cover_pct=cloud[i],
                pressure_msl_hpa=press[i],
                wind_speed_kmh=wind_s[i],
                wind_direction_deg=wind_d[i],
                wind_gusts_kmh=wind_g[i],
                weather_code=code[i],
                weather_description=describe_weather_code(code[i]),
            )
        )
    return out


def _daily_from_payload(daily: dict[str, Any]) -> list[DailyAggregate]:
    """Pivot Open-Meteo `daily` column-arrays to a list of DailyAggregate rows."""
    dates: list[str] = daily.get("time") or []
    n = len(dates)

    def col(name: str) -> list[Any]:
        return daily.get(name) or [None] * n

    tmax = col("temperature_2m_max")
    tmin = col("temperature_2m_min")
    app_max = col("apparent_temperature_max")
    app_min = col("apparent_temperature_min")
    precip = col("precipitation_sum")
    rain = col("rain_sum")
    wind_max = col("wind_speed_10m_max")
    gust_max = col("wind_gusts_10m_max")
    code = col("weather_code")
    sun = col("sunshine_duration")

    out: list[DailyAggregate] = []
    for i in range(n):
        out.append(
            DailyAggregate(
                date=dates[i],
                temperature_max_c=tmax[i],
                temperature_min_c=tmin[i],
                apparent_temperature_max_c=app_max[i],
                apparent_temperature_min_c=app_min[i],
                precipitation_sum_mm=precip[i],
                rain_sum_mm=rain[i],
                wind_speed_max_kmh=wind_max[i],
                wind_gusts_max_kmh=gust_max[i],
                weather_code=code[i],
                weather_description=describe_weather_code(code[i]),
                sunshine_duration_s=sun[i],
            )
        )
    return out


def build_response(
    *,
    location: ResolvedLocation,
    payload: dict[str, Any],
    source_url: str,
    user_query: dict[str, Any],
    start_period: str | None = None,
    end_period: str | None = None,
) -> WeatherResponse:
    """Build the typed envelope. Pivots whichever of current/hourly/daily
    Open-Meteo returned. Always sets retrieved_at + server_version + the
    full attribution string.

    `location` is a ResolvedLocation (from resolution.resolve_location).
    For curated lookups, location.curated_id is the curated YAML key; for
    geocoded/coord-based lookups it's None. Either way every response
    carries the resolved name + lat/lng + the resolution-source marker
    so the agent (and the user) can see HOW the input was resolved."""
    current = payload.get("current")
    hourly = payload.get("hourly")
    daily = payload.get("daily")

    current_obs = _current_from_payload(current) if current else None
    hourly_obs = _hourly_from_payload(hourly) if hourly else None
    daily_obs = _daily_from_payload(daily) if daily else None

    # Derive period bounds if caller didn't supply them — look at the
    # actual times we got back so the response is always self-consistent.
    # Inside each branch the corresponding list/obs is already truthy, so
    # no need for inner `if x else None` guards.
    if not start_period or not end_period:
        if daily_obs:
            start_period = start_period or daily_obs[0].date
            end_period = end_period or daily_obs[-1].date
        elif hourly_obs:
            start_period = start_period or hourly_obs[0].time
            end_period = end_period or hourly_obs[-1].time
        elif current_obs:
            start_period = start_period or current_obs.time
            end_period = end_period or current_obs.time

    # Attribution adapts to what we actually used. Default covers
    # Open-Meteo + BOM. When the location was resolved via Nominatim
    # (postcode lookup), OSM attribution is required by their ODbL licence.
    attribution = DEFAULT_ATTRIBUTION
    if location.source == "postcode":
        attribution += OSM_ATTRIBUTION_SUFFIX

    return WeatherResponse(
        location_id=location.curated_id,
        location_name=location.name,
        state=location.state,
        latitude=location.latitude,
        longitude=location.longitude,
        timezone=location.timezone,
        location_resolution=location.source,
        location_input=location.original_input,
        query=user_query,
        period={"start": start_period, "end": end_period},
        current=current_obs,
        hourly=hourly_obs,
        daily=daily_obs,
        attribution=attribution,
        source_url=source_url,
        retrieved_at=datetime.now(timezone.utc),
        server_version=__version__,
        stale=False,
        stale_reason=None,
    )


# ---------- v0.4.0: air quality ----------

# European AQI bands per Copernicus CAMS classification.
_EU_AQI_LABELS = [
    (20, "Good"),
    (40, "Fair"),
    (60, "Moderate"),
    (80, "Poor"),
    (100, "Very poor"),
    (float("inf"), "Extremely poor"),
]
# US AQI bands per EPA.
_US_AQI_LABELS = [
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for sensitive groups"),
    (200, "Unhealthy"),
    (300, "Very unhealthy"),
    (float("inf"), "Hazardous"),
]


def _label_aqi(value: int | None, bands: list[tuple[float, str]]) -> str | None:
    """Map an integer AQI to the relevant band's plain-English label."""
    if value is None:
        return None
    for upper, label in bands:
        if value <= upper:
            return label
    return None


def build_air_quality_response(
    *,
    location: ResolvedLocation,
    payload: dict[str, Any],
    source_url: str,
) -> AirQualityResponse:
    """Build the AirQualityResponse envelope. Same trust contract as
    build_response — source_url, attribution, retrieved_at, server_version."""
    # Open-Meteo returns the current block only when at least one variable
    # was requested + returned. If `current` is missing or empty we surface
    # `observation = None` so the agent can distinguish "no data" from
    # "all values were null".
    current = payload.get("current")
    observation: AirQualityObservation | None
    if current:
        eu_aqi = current.get("european_aqi")
        us_aqi = current.get("us_aqi")
        observation = AirQualityObservation(
            time=str(current.get("time", "")),
            pm2_5_ugm3=current.get("pm2_5"),
            pm10_ugm3=current.get("pm10"),
            ozone_ugm3=current.get("ozone"),
            nitrogen_dioxide_ugm3=current.get("nitrogen_dioxide"),
            sulphur_dioxide_ugm3=current.get("sulphur_dioxide"),
            carbon_monoxide_ugm3=current.get("carbon_monoxide"),
            european_aqi=eu_aqi,
            us_aqi=us_aqi,
            european_aqi_label=_label_aqi(eu_aqi, _EU_AQI_LABELS),
            us_aqi_label=_label_aqi(us_aqi, _US_AQI_LABELS),
        )
    else:
        observation = None

    # OSM attribution carries over when the location was postcode-resolved
    attribution = DEFAULT_ATTRIBUTION
    if location.source == "postcode":
        attribution += OSM_ATTRIBUTION_SUFFIX

    return AirQualityResponse(
        location_id=location.curated_id,
        location_name=location.name,
        state=location.state,
        latitude=location.latitude,
        longitude=location.longitude,
        timezone=location.timezone,
        location_resolution=location.source,
        location_input=location.original_input,
        current=observation,
        attribution=attribution,
        source_url=source_url,
        retrieved_at=datetime.now(timezone.utc),
        server_version=__version__,
        stale=False,
        stale_reason=None,
    )
