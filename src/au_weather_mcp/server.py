"""FastMCP server entrypoint for au-weather-mcp.

Five tools, all thin orchestrators over `client`, `curated`, and `shaping`.
The shared OpenMeteoClient is created lazily so importing this module doesn't
open the SQLite cache.

Trust contract:
- Every response carries source_url + attribution + retrieved_at + server_version
- Every error path raises ValueError with an actionable hint
- Pydantic sanity validators (in models.py) catch out-of-range values from
  upstream rather than silently passing them through
- URL-safety patterns reject injection characters in location IDs and periods
"""
from __future__ import annotations

import asyncio
import re
from datetime import date as _date, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import curated as curated_mod
from .client import OpenMeteoClient, OpenMeteoError
from .models import LocationDetail, LocationSummary, WeatherResponse
from .shaping import build_response

# Location IDs are lowercase letters + digits + underscore.
_LOCATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Date strings: strictly YYYY-MM-DD. Anything else fails URL safety.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Default variable bundles — what Open-Meteo will return when caller doesn't
# specify. Picked to cover the common LLM-agent use cases (temp, rain, wind,
# pressure) without overwhelming smaller responses.
_DEFAULT_CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "rain,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,"
    "wind_gusts_10m,weather_code"
).split(",")
_DEFAULT_HOURLY_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "rain,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,"
    "wind_gusts_10m,weather_code"
).split(",")
_DEFAULT_DAILY_VARS = (
    "temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "apparent_temperature_min,precipitation_sum,rain_sum,wind_speed_10m_max,"
    "wind_gusts_10m_max,weather_code,sunshine_duration"
).split(",")

mcp = FastMCP("au-weather-mcp")

_client: OpenMeteoClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> OpenMeteoClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = OpenMeteoClient()
        return _client


async def reset_client_for_tests() -> None:
    """Drop the cached client. Tests that span event loops must clear it
    or httpx trips on a closed loop."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _normalize_location_id(location: Any) -> str:
    if not isinstance(location, str):
        raise ValueError(
            f"location must be a string, got {type(location).__name__}. "
            "Try list_curated() to discover IDs like 'sydney', 'melbourne', or 'cairns'."
        )
    normalized = location.strip().lower()
    if not normalized:
        raise ValueError(
            "location is empty. Try list_curated() to discover IDs like "
            "'sydney', 'melbourne', or 'cairns'."
        )
    if not _LOCATION_ID_PATTERN.match(normalized):
        raise ValueError(
            f"location {location!r} contains invalid characters — "
            "location IDs are lowercase letters, digits, and underscores "
            "(e.g. 'sydney', 'gold_coast'). Try search_locations() to find IDs."
        )
    return normalized


def _validate_date(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{name} must be a string in 'YYYY-MM-DD' format, got {type(value).__name__}."
        )
    s = value.strip()
    if not s:
        return None
    if not _DATE_PATTERN.match(s):
        raise ValueError(
            f"{name} {value!r} has invalid format. Use 'YYYY-MM-DD' (e.g. '2024-03-15')."
        )
    # Semantic check: '2024-13-40' matches the regex but isn't a real date.
    try:
        _date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(
            f"{name} {value!r} is not a valid date: {e}. Use 'YYYY-MM-DD' (e.g. '2024-03-15')."
        )
    return s


def _resolve_location(location_id: str) -> curated_mod.CuratedLocation:
    loc = curated_mod.get(location_id)
    if loc is None:
        valid = curated_mod.list_ids()
        raise ValueError(
            f"Unknown location {location_id!r}. "
            f"Try one of: {', '.join(valid[:10])}"
            + ("..." if len(valid) > 10 else "")
            + ". Use search_locations() for fuzzy lookup."
        )
    return loc


# ---------- Tools ----------

@mcp.tool
async def search_locations(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text search query. Matches against location IDs, names, "
                "and state codes. Case-insensitive."
            ),
            examples=["sydney", "nsw", "tropical north", "gold coast", "tasmania"],
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results to return, ranked by relevance.",
            examples=[5, 10, 21],
            ge=1,
            le=100,
        ),
    ] = 10,
) -> list[LocationSummary]:
    """Fuzzy-search the 21 curated Australian locations.

    The curated set covers all 8 state/territory capitals plus 13 major
    regional centres (Newcastle, Wollongong, Gold Coast, Sunshine Coast,
    Cairns, Townsville, Mackay, Geelong, Ballarat, Bendigo, Launceston,
    Alice Springs, Broome).

    Examples:
        results = await search_locations("sydney")
        # → [{id: 'sydney', name: 'Sydney', state: 'NSW', ...}]

        results = await search_locations("nsw")
        # → Newcastle, Wollongong, Sydney (all NSW locations)

    When to use:
        - Discover the location ID for a city you know by name
        - Find all supported locations in a state
        - Verify whether a place is in the curated set before calling get_weather

    Returns:
        List of LocationSummary (id, name, state, description), ranked by
        relevance.
    """
    if not isinstance(query, str):
        raise ValueError(
            f"query must be a string, got {type(query).__name__}. "
            "Try 'sydney', 'nsw', 'tropical', or another place name."
        )
    if not query.strip():
        raise ValueError(
            "query is required. Try 'sydney', 'nsw', 'tropical', "
            "or any Australian place name."
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be a positive integer, got {limit!r} ({type(limit).__name__})."
        )
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}.")
    matches = curated_mod.search(query, limit=limit)
    return [
        LocationSummary(
            id=m.id,
            name=m.name,
            state=m.state,
            description=m.description,
        )
        for m in matches
    ]


@mcp.tool
async def describe_location(
    location: Annotated[
        str,
        Field(
            description=(
                "Curated location ID like 'sydney', 'melbourne', 'cairns'. "
                "Use search_locations() or list_curated() to discover. "
                "Case-insensitive ('Sydney', 'SYDNEY', ' sydney ' all work)."
            ),
            examples=["sydney", "melbourne", "cairns", "alice_springs"],
        ),
    ],
) -> LocationDetail:
    """Return full metadata for a curated location — lat/lng, timezone,
    elevation, nearest BOM station, and the canonical Open-Meteo URL.

    Examples:
        detail = await describe_location("sydney")
        # detail.latitude == -33.8607
        # detail.longitude == 151.2050
        # detail.timezone == 'Australia/Sydney'
        # detail.nearest_bom_station == '066062 (Sydney Observatory Hill)'

    When to use:
        - Before calling get_weather, to confirm the location's coordinates
          and timezone
        - To cross-reference with BOM's own observation network
        - To get a direct Open-Meteo URL for citation in agent responses

    Returns:
        LocationDetail with id, name, state, lat/lng, timezone, elevation,
        nearest BOM station ID, the Open-Meteo URL, and the CC-BY attribution.
    """
    location_id = _normalize_location_id(location)
    loc = _resolve_location(location_id)
    open_meteo_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={loc.latitude}"
        f"&longitude={loc.longitude}&timezone={loc.timezone}&current=temperature_2m"
    )
    attribution = (
        "Weather data by Open-Meteo.com (https://open-meteo.com), licensed under "
        "CC BY 4.0. Underlying data includes the Australian Bureau of Meteorology "
        "(https://www.bom.gov.au) under Open-Meteo's licensing arrangement."
    )
    return LocationDetail(
        id=loc.id,
        name=loc.name,
        state=loc.state,
        description=loc.description,
        latitude=loc.latitude,
        longitude=loc.longitude,
        timezone=loc.timezone,
        elevation_m=loc.elevation_m,
        nearest_bom_station=loc.nearest_bom_station,
        open_meteo_url=open_meteo_url,
        attribution=attribution,
    )


@mcp.tool
async def latest(
    location: Annotated[
        str,
        Field(
            description=(
                "Curated location ID. Use list_curated() to enumerate the 21 "
                "supported AU locations, or search_locations() for fuzzy lookup."
            ),
            examples=["sydney", "melbourne", "brisbane", "perth", "darwin"],
        ),
    ],
) -> WeatherResponse:
    """Return the current weather observation for a curated location.

    Wraps Open-Meteo's `/forecast` endpoint with `current=...` parameters
    and a 15-minute cache TTL (matches Open-Meteo's own update cadence).
    Use this for "what's the weather right now?" questions — warm-cache
    latency target < 50 ms.

    Examples:
        resp = await latest("sydney")
        # resp.current.temperature_c == 19.7
        # resp.current.relative_humidity_pct == 67
        # resp.current.wind_speed_kmh == 18.4
        # resp.current.weather_description == 'Mainly clear'

        resp = await latest("cairns")
        # → tropical reading: ~28°C, 80% humidity in May

    When to use:
        - "What's the weather right now in <city>?" — the canonical use case
        - Building a multi-city current-conditions dashboard
        - Anchoring a longer agent conversation to live weather context

    Returns:
        WeatherResponse with `current` populated (single WeatherObservation),
        plus location metadata, source_url for citation, the CC-BY attribution,
        and the server version.
    """
    location_id = _normalize_location_id(location)
    loc = _resolve_location(location_id)
    client = await _get_client()
    try:
        payload = await client.forecast(
            latitude=loc.latitude,
            longitude=loc.longitude,
            timezone=loc.timezone,
            current=_DEFAULT_CURRENT_VARS,
            forecast_days=1,
            kind="current",
        )
    except OpenMeteoError as e:
        raise ValueError(
            f"Could not fetch current weather for {location_id}. "
            f"Try describe_location('{location_id}') to verify coordinates. ({e})"
        ) from e
    # Reconstruct the URL we hit, for source_url citation
    source_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={loc.latitude}"
        f"&longitude={loc.longitude}&timezone={loc.timezone}"
        f"&current={','.join(_DEFAULT_CURRENT_VARS)}&forecast_days=1"
    )
    return build_response(
        location=loc,
        payload=payload,
        source_url=source_url,
        user_query={"location": location_id},
    )


@mcp.tool
async def get_weather(
    location: Annotated[
        str,
        Field(
            description="Curated location ID like 'sydney'. Use list_curated() to discover.",
            examples=["sydney", "melbourne", "brisbane"],
        ),
    ],
    start_date: Annotated[
        str | None,
        Field(
            description=(
                "Inclusive start date in 'YYYY-MM-DD' format. Open-Meteo's "
                "historical archive covers 1940-01-01 onwards (5-day lag). "
                "Forecast covers today through today + 16 days."
            ),
            examples=["2024-01-01", "2024-06-15", "2026-05-10"],
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Field(
            description="Inclusive end date in 'YYYY-MM-DD' format. Same range rules as start_date.",
            examples=["2024-12-31", "2024-06-30", "2026-05-17"],
        ),
    ] = None,
    granularity: Annotated[
        Literal["daily", "hourly"],
        Field(
            description=(
                "Time resolution of the returned series. 'daily' (default) "
                "returns one row per day with max/min/sum aggregates. "
                "'hourly' returns one row per hour with point observations — "
                "useful for intraday detail but expect ~24× more records."
            ),
            examples=["daily", "hourly"],
        ),
    ] = "daily",
) -> WeatherResponse:
    """Query weather over a date range. Routes to historical archive or
    forecast endpoint automatically based on the date range.

    Routing logic:
        - end_date in the past (>= 5 days ago) → historical archive (1940+)
        - start_date in the future → forecast (today + 16 days max)
        - range straddles today → forecast with past_days set

    Examples:
        # Historical: how was Sydney summer 2020?
        resp = await get_weather(
            "sydney",
            start_date="2020-01-01",
            end_date="2020-01-31",
            granularity="daily",
        )
        # → 31 DailyAggregate rows with temp_max, temp_min, precip per day

        # 7-day forecast for Melbourne, hourly detail
        resp = await get_weather(
            "melbourne",
            start_date="2026-05-12",
            end_date="2026-05-19",
            granularity="hourly",
        )
        # → 168 hourly WeatherObservation rows

        # Just today (omit both dates)
        resp = await get_weather("brisbane")
        # → today's daily aggregate

    When to use:
        - Time-series queries (forecast over the next week, or historical
          comparison)
        - Multi-day weather analysis
        - Climate research and historical look-backs (decade-scale via archive)

    Returns:
        WeatherResponse with either `daily` or `hourly` populated depending
        on `granularity`. Period bounds populated from actual returned data.
    """
    location_id = _normalize_location_id(location)
    start_validated = _validate_date(start_date, "start_date")
    end_validated = _validate_date(end_date, "end_date")
    if start_validated and end_validated and start_validated > end_validated:
        raise ValueError(
            f"end_date ({end_validated}) is before start_date ({start_validated}). "
            "Try swapping them."
        )
    if granularity not in ("daily", "hourly"):
        raise ValueError(
            f"granularity must be 'daily' or 'hourly', got {granularity!r}."
        )
    loc = _resolve_location(location_id)
    client = await _get_client()

    today = _date.today()
    # If no dates supplied, return today's data via forecast
    if not start_validated and not end_validated:
        start_validated = today.isoformat()
        end_validated = today.isoformat()
    elif start_validated and not end_validated:
        end_validated = start_validated
    elif end_validated and not start_validated:
        start_validated = end_validated

    start_d = _date.fromisoformat(start_validated)
    end_d = _date.fromisoformat(end_validated)
    archive_cutoff = today - timedelta(days=5)
    use_archive = end_d <= archive_cutoff

    vars_param = _DEFAULT_DAILY_VARS if granularity == "daily" else _DEFAULT_HOURLY_VARS
    daily_arg = vars_param if granularity == "daily" else None
    hourly_arg = vars_param if granularity == "hourly" else None

    try:
        if use_archive:
            payload = await client.archive(
                latitude=loc.latitude,
                longitude=loc.longitude,
                timezone=loc.timezone,
                start_date=start_validated,
                end_date=end_validated,
                hourly=hourly_arg,
                daily=daily_arg,
            )
            base = "https://archive-api.open-meteo.com/v1/archive"
        else:
            payload = await client.forecast(
                latitude=loc.latitude,
                longitude=loc.longitude,
                timezone=loc.timezone,
                hourly=hourly_arg,
                daily=daily_arg,
                start_date=start_validated,
                end_date=end_validated,
            )
            base = "https://api.open-meteo.com/v1/forecast"
    except OpenMeteoError as e:
        raise ValueError(
            f"Could not fetch weather for {location_id} between {start_validated} "
            f"and {end_validated}. ({e})"
        ) from e

    source_url = (
        f"{base}?latitude={loc.latitude}&longitude={loc.longitude}"
        f"&timezone={loc.timezone}&start_date={start_validated}&end_date={end_validated}"
        f"&{granularity}={','.join(vars_param)}"
    )
    return build_response(
        location=loc,
        payload=payload,
        source_url=source_url,
        user_query={
            "location": location_id,
            "start_date": start_validated,
            "end_date": end_validated,
            "granularity": granularity,
        },
        start_period=start_validated,
        end_period=end_validated,
    )


@mcp.tool
def list_curated() -> list[str]:
    """List the 21 curated Australian location IDs supported by this MCP.

    The curated set covers all 8 state/territory capitals plus 13 major
    regional centres:
        - 8 capitals: sydney, melbourne, brisbane, perth, adelaide, hobart,
          darwin, canberra
        - 5 NSW regional: newcastle, wollongong (NSW capitals as above)
        - 5 QLD regional: gold_coast, sunshine_coast, cairns, townsville, mackay
        - 3 VIC regional: geelong, ballarat, bendigo
        - 1 TAS regional: launceston
        - 2 remote: alice_springs (NT), broome (WA)

    Example:
        ids = list_curated()
        # → ['adelaide', 'alice_springs', 'ballarat', 'bendigo', 'brisbane',
        #    'broome', 'cairns', 'canberra', 'darwin', 'geelong', 'gold_coast',
        #    'hobart', 'launceston', 'mackay', 'melbourne', 'newcastle',
        #    'perth', 'sunshine_coast', 'sydney', 'townsville', 'wollongong']

    When to use:
        - You want to know which locations have first-class support
        - You're building a UI that shows the supported set up front
        - You want to plan a multi-location dashboard call

    Returns:
        Sorted list of location IDs. Always 21 entries today; adding a
        location is a YAML edit, not a code change.
    """
    return curated_mod.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
