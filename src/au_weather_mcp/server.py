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
from datetime import date as _date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastmcp import FastMCP
from pydantic import Field

from . import curated as curated_mod
from . import __version__
from .client import (
    OpenMeteoClient,
    OpenMeteoError,
    get_stale_signal,
    reset_stale_signal,
)
from .models import (
    DEFAULT_ATTRIBUTION,
    AirQualityResponse,
    ComparisonResponse,
    ComparisonRow,
    LocationDetail,
    LocationSummary,
    WeatherResponse,
)
from .resolution import ResolvedLocation, resolve_location
from .shaping import build_air_quality_response, build_response

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
# Hourly query asks for the same point-in-time variables as the "current"
# block — just sampled at every hour instead of once. Same list either way.
_DEFAULT_HOURLY_VARS = _DEFAULT_CURRENT_VARS
_DEFAULT_AIR_QUALITY_VARS = (
    "pm10,pm2_5,european_aqi,us_aqi,carbon_monoxide,nitrogen_dioxide,"
    "sulphur_dioxide,ozone"
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
    # Fast path: once initialised, no need to take the lock — every tool
    # call hits this. Mirrors the double-checked pattern in cache.py.
    if _client is not None:
        return _client
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


async def _resolve(client: OpenMeteoClient, location: Any) -> ResolvedLocation:
    """Single entry to the resolution layer. Raises ValueError on bad input."""
    return await resolve_location(client, location)


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
    """Fuzzy-search the 45 curated Australian locations.

    The curated set covers all 8 state/territory capitals plus 37 major
    regional centres (every AU population centre over ~25k). Anything
    outside the curated set still resolves via place-name geocoding or
    postcode lookup — see `list_curated()` for the full set.

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
                "Any Australian location, in any of these shapes: "
                "(1) curated ID like 'sydney', 'gold_coast'; "
                "(2) place name in any case like 'Sydney', 'Gold Coast', 'Margaret River', 'Byron Bay'; "
                "(3) state code or name like 'NSW', 'VIC', 'Queensland' (returns the state capital); "
                "(4) raw coordinates like '-33.87,151.21'. "
                "Case-insensitive throughout."
            ),
            examples=[
                "sydney",
                "Sydney",
                "Margaret River",
                "NSW",
                "Queensland",
                "-33.87,151.21",
                "Byron Bay",
            ],
        ),
    ],
) -> LocationDetail:
    """Return metadata for an Australian location — name, lat/lng, timezone,
    elevation, and (for curated locations) the nearest BOM station ID.

    Accepts a wide range of input shapes for compatibility — see the
    `location` parameter description. The returned `id` is None for
    non-curated lookups (geocoded place names, raw coordinates) and a
    snake_case curated key when the input matched the curated set.

    Examples:
        await describe_location("sydney")           # → curated path
        await describe_location("Sydney")           # → curated path (case-insensitive)
        await describe_location("NSW")              # → state capital (Sydney)
        await describe_location("Margaret River")   # → geocoded (Western Australia)
        await describe_location("-33.87,151.21")    # → raw coordinates

    When to use:
        - Before calling get_weather, to confirm coordinates and timezone
        - To cross-reference with BOM's own observation network (for curated)
        - To verify how the server resolved an ambiguous customer input

    Returns:
        LocationDetail with id (or None), name, state, lat/lng, timezone,
        elevation, nearest BOM station ID (curated only), the Open-Meteo
        URL, and the CC-BY attribution.
    """
    client = await _get_client()
    resolved = await _resolve(client, location)
    open_meteo_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={resolved.latitude}"
        f"&longitude={resolved.longitude}&timezone={resolved.timezone}&current=temperature_2m"
    )
    attribution = DEFAULT_ATTRIBUTION
    # nearest_bom_station + description live on the curated YAML row only
    nearest_bom = None
    description = None
    if resolved.curated_id is not None:
        cd = curated_mod.get(resolved.curated_id)
        if cd is not None:
            nearest_bom = cd.nearest_bom_station
            description = cd.description
    if description is None:
        description = f"Resolved via {resolved.source} from input {resolved.original_input!r}"
    return LocationDetail(
        id=resolved.curated_id,
        name=resolved.name,
        state=resolved.state or "?",
        description=description,
        latitude=resolved.latitude,
        longitude=resolved.longitude,
        timezone=resolved.timezone,
        elevation_m=resolved.elevation_m,
        nearest_bom_station=nearest_bom,
        open_meteo_url=open_meteo_url,
        attribution=attribution,
    )


@mcp.tool
async def latest(
    location: Annotated[
        str,
        Field(
            description=(
                "Any Australian location. Accepted shapes: curated ID ('sydney'), "
                "place name in any case ('Sydney', 'Byron Bay', 'Margaret River'), "
                "state code or name ('NSW', 'Queensland' → returns the capital), "
                "or raw coordinates ('-33.87,151.21')."
            ),
            examples=[
                "sydney",
                "Sydney",
                "Byron Bay",
                "NSW",
                "Queensland",
                "-33.87,151.21",
            ],
        ),
    ],
) -> WeatherResponse:
    """Return the current weather observation for any Australian location.

    Wraps Open-Meteo's `/forecast` endpoint with `current=...` parameters
    and a 15-minute cache TTL (matches Open-Meteo's own update cadence).
    Use for "what's the weather right now?" — warm-cache latency < 100 ms.

    Examples:
        resp = await latest("sydney")               # curated, fast path
        resp = await latest("Sydney")               # case-insensitive
        resp = await latest("Byron Bay")            # geocoded
        resp = await latest("NSW")                  # state → Sydney
        resp = await latest("-33.87,151.21")        # raw coordinates

    The response's `location_resolution` field tells the agent how the
    input was interpreted ('curated', 'state_alias', 'geocoded',
    'raw_coordinates', or 'fuzzy_curated').

    When to use:
        - "What's the weather right now in <place>?" — canonical use case
        - Multi-city current-conditions dashboards (call once per place)
        - Anchoring agent conversations to live weather context

    Returns:
        WeatherResponse with `current` populated, plus location metadata,
        resolution source, source_url, CC-BY attribution, and server_version.
    """
    # Reset the graceful-degradation flag at the start of each tool call so
    # we only report staleness introduced by THIS call's fetches.
    reset_stale_signal()
    client = await _get_client()
    resolved = await _resolve(client, location)
    try:
        payload = await client.forecast(
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            timezone=resolved.timezone,
            current=_DEFAULT_CURRENT_VARS,
            forecast_days=1,
            kind="current",
        )
    except OpenMeteoError as e:
        raise ValueError(
            f"Could not fetch current weather for {location!r} "
            f"(resolved to {resolved.name}). "
            f"Try describe_location({location!r}) to verify coordinates. ({e})"
        ) from e
    # source_url via urlencode so it byte-matches the URL client.forecast()
    # actually hit (audit-flagged round-trip fidelity; symmetric with the
    # air_quality + compare_locations fix from v0.4.1).
    source_url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urlencode({
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "timezone": resolved.timezone,
            "current": ",".join(_DEFAULT_CURRENT_VARS),
            "forecast_days": 1,
        })
    )
    resp = build_response(
        location=resolved,
        payload=payload,
        source_url=source_url,
        user_query={"location": location},
    )
    # If any fetch in the chain (resolve or forecast) served a stale-cache
    # fallback because Open-Meteo was unreachable, propagate to the response.
    stale, reason = get_stale_signal()
    if stale:
        resp.stale = True
        resp.stale_reason = reason
    return resp


@mcp.tool
async def get_weather(
    location: Annotated[
        str,
        Field(
            description=(
                "Any Australian location. Same accepted shapes as latest(): "
                "curated ID, place name (any case), state code/name, or raw "
                "'lat,lng' coordinates."
            ),
            examples=[
                "sydney",
                "Margaret River",
                "NSW",
                "Byron Bay",
                "-33.87,151.21",
            ],
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
    # Reset the graceful-degradation flag at the start of each tool call so
    # we only report staleness introduced by THIS call's fetches.
    reset_stale_signal()
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
    client = await _get_client()
    resolved = await _resolve(client, location)

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
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
                start_date=start_validated,
                end_date=end_validated,
                hourly=hourly_arg,
                daily=daily_arg,
            )
            base = "https://archive-api.open-meteo.com/v1/archive"
        else:
            payload = await client.forecast(
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
                hourly=hourly_arg,
                daily=daily_arg,
                start_date=start_validated,
                end_date=end_validated,
            )
            base = "https://api.open-meteo.com/v1/forecast"
    except OpenMeteoError as e:
        raise ValueError(
            f"Could not fetch weather for {location!r} (resolved to {resolved.name}) "
            f"between {start_validated} and {end_validated}. ({e})"
        ) from e

    # source_url via urlencode so it byte-matches the URL the client actually
    # hit (round-trip fidelity; symmetric with v0.4.1's air_quality fix).
    source_url = f"{base}?" + urlencode({
        "latitude": resolved.latitude,
        "longitude": resolved.longitude,
        "timezone": resolved.timezone,
        "start_date": start_validated,
        "end_date": end_validated,
        granularity: ",".join(vars_param),
    })
    resp = build_response(
        location=resolved,
        payload=payload,
        source_url=source_url,
        user_query={
            "location": location,
            "start_date": start_validated,
            "end_date": end_validated,
            "granularity": granularity,
        },
        start_period=start_validated,
        end_period=end_validated,
    )
    stale, reason = get_stale_signal()
    if stale:
        resp.stale = True
        resp.stale_reason = reason
    return resp


@mcp.tool
async def air_quality(
    location: Annotated[
        str,
        Field(
            description=(
                "Any Australian location. Same accepted shapes as latest(): "
                "curated ID, place name, state code/name, postcode, or "
                "'lat,lng' coordinates."
            ),
            examples=[
                "sydney",
                "Sydney",
                "Byron Bay",
                "2026",
                "NSW",
                "-33.87,151.21",
            ],
        ),
    ],
) -> AirQualityResponse:
    """Return current air-quality readings for any Australian location.

    Sourced from Open-Meteo's air-quality API, which merges Copernicus CAMS
    European + global air-composition models. Returns PM2.5, PM10, ozone,
    nitrogen dioxide, sulphur dioxide, carbon monoxide (all µg/m³), plus the
    European and US AQI indices with plain-English labels.

    Especially useful during AU bushfire season (Oct–Mar) when smoke can
    push PM2.5 above safe levels across whole regions.

    Examples:
        # Current Sydney air quality
        resp = await air_quality("sydney")
        # resp.current.pm2_5_ugm3 == 8.8
        # resp.current.european_aqi == 21
        # resp.current.european_aqi_label == 'Good'
        # resp.current.us_aqi == 39
        # resp.current.us_aqi_label == 'Good'

        # Bushfire smoke check for the Blue Mountains
        resp = await air_quality("-33.7,150.3")

        # Brisbane CBD via postcode
        resp = await air_quality("4000")

    When to use:
        - "Is the air clean enough to go for a run in <city>?"
        - Bushfire smoke or burn-off impact checks
        - Asthma / allergy planning
        - Long-term air quality monitoring (call periodically and chart)

    Returns:
        AirQualityResponse with `current` populated (pollutants + AQI scales),
        plus location metadata, source_url, attribution, and server_version.
    """
    reset_stale_signal()
    client = await _get_client()
    resolved = await _resolve(client, location)
    try:
        payload = await client.air_quality(
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            timezone=resolved.timezone,
            current=_DEFAULT_AIR_QUALITY_VARS,
        )
    except OpenMeteoError as e:
        raise ValueError(
            f"Could not fetch air quality for {location!r} (resolved to "
            f"{resolved.name}). ({e})"
        ) from e
    # Build source_url via the same urlencode the client uses so the URL
    # advertised in the response is byte-identical to the URL Open-Meteo
    # actually served (audit-flagged round-trip fidelity).
    source_url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality?"
        + urlencode({
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "timezone": resolved.timezone,
            "current": ",".join(_DEFAULT_AIR_QUALITY_VARS),
        })
    )
    resp = build_air_quality_response(
        location=resolved,
        payload=payload,
        source_url=source_url,
    )
    stale, reason = get_stale_signal()
    if stale:
        resp.stale = True
        resp.stale_reason = reason
    return resp


@mcp.tool
async def compare_locations(
    locations: Annotated[
        list[str],
        Field(
            description=(
                "Two to ten Australian locations to compare side-by-side. Each "
                "entry accepts the same shapes as latest(): curated ID, place "
                "name, state, postcode, or 'lat,lng'."
            ),
            examples=[
                ["sydney", "melbourne", "brisbane", "perth"],
                ["Cairns", "Hobart", "Darwin"],
                ["2026", "3000", "4000"],
            ],
            min_length=2,
            max_length=10,
        ),
    ],
) -> ComparisonResponse:
    """Compare current weather across multiple Australian locations in one call.

    Fans out concurrently via asyncio.gather, so all locations come back in
    roughly the time of a single call (after the cache is warm). Each location
    is independently resolved (curated / state / postcode / geocode / etc.) and
    independently fetched. If one location fails (e.g. geocoder can't find it),
    that row gets an `error` field while the rest still return.

    Examples:
        # Capital-city dashboard
        resp = await compare_locations(["sydney","melbourne","brisbane","perth"])
        for row in resp.locations:
            print(row.location_name, row.current.temperature_c)

        # Tropical north today
        resp = await compare_locations(["Cairns","Darwin","Townsville","Broome"])

        # Mixed input shapes work
        resp = await compare_locations(["sydney","NSW","2026","-33.87,151.21"])

    When to use:
        - "Compare weather in <several cities>" — the canonical use case
        - Build a multi-region dashboard in one tool call
        - Plan a holiday across regions

    Returns:
        ComparisonResponse with one ComparisonRow per input location.
        Successful rows have `current` populated; failed rows have `error`.
    """
    # Validation: list of 2-10 non-empty strings
    if not isinstance(locations, list):
        raise ValueError(
            f"locations must be a list of strings, got {type(locations).__name__}."
        )
    if len(locations) < 2:
        raise ValueError(
            f"compare_locations requires at least 2 locations, got {len(locations)}. "
            "Use latest() for a single location."
        )
    if len(locations) > 10:
        raise ValueError(
            f"compare_locations supports at most 10 locations, got {len(locations)}. "
            "Make multiple calls for larger comparisons."
        )

    client = await _get_client()

    async def _resolve_and_fetch(loc_input: str) -> ComparisonRow:
        """One row: resolve + fetch + wrap.

        Trust contract: one row failing must NOT poison sibling rows in
        the gather. We catch ValueError and OpenMeteoError specifically
        (for nicer error messages) AND a broad Exception fallback so any
        unanticipated upstream issue (pydantic ValidationError from a
        sanity-check tripping on bad upstream values, an httpx error not
        wrapped as OpenMeteoError, etc.) becomes an error on the row
        rather than crashing the whole call.
        """
        try:
            resolved = await _resolve(client, loc_input)
        except ValueError as e:
            return ComparisonRow(
                location_id=None,
                location_name=str(loc_input),
                state=None,
                latitude=0.0,
                longitude=0.0,
                timezone="UTC",
                location_resolution="curated",  # placeholder; ignored when error set
                location_input=str(loc_input),
                current=None,
                source_url="",
                error=f"could not resolve location: {e}",
            )
        except Exception as e:  # noqa: BLE001 — deliberate broad isolation barrier
            return ComparisonRow(
                location_id=None,
                location_name=str(loc_input),
                state=None,
                latitude=0.0,
                longitude=0.0,
                timezone="UTC",
                location_resolution="curated",
                location_input=str(loc_input),
                current=None,
                source_url="",
                error=f"unexpected resolve error ({type(e).__name__}): {e}",
            )
        # Build source_url via urlencode so it byte-matches the URL the
        # client actually hits (audit-flagged round-trip fidelity).
        source_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urlencode({
                "latitude": resolved.latitude,
                "longitude": resolved.longitude,
                "timezone": resolved.timezone,
                "current": ",".join(_DEFAULT_CURRENT_VARS),
                "forecast_days": 1,
            })
        )
        try:
            payload = await client.forecast(
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
                current=_DEFAULT_CURRENT_VARS,
                forecast_days=1,
                kind="current",
            )
            resp = build_response(
                location=resolved,
                payload=payload,
                source_url=source_url,
                user_query={"location": loc_input},
            )
        except OpenMeteoError as e:
            return ComparisonRow(
                location_id=resolved.curated_id,
                location_name=resolved.name,
                state=resolved.state,
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
                location_resolution=resolved.source,
                location_input=resolved.original_input,
                current=None,
                source_url=source_url,
                error=f"upstream fetch failed: {e}",
            )
        except Exception as e:  # noqa: BLE001 — isolation barrier; see docstring
            return ComparisonRow(
                location_id=resolved.curated_id,
                location_name=resolved.name,
                state=resolved.state,
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
                location_resolution=resolved.source,
                location_input=resolved.original_input,
                current=None,
                source_url=source_url,
                error=f"unexpected fetch/parse error ({type(e).__name__}): {e}",
            )
        return ComparisonRow(
            location_id=resp.location_id,
            location_name=resp.location_name,
            state=resp.state,
            latitude=resp.latitude,
            longitude=resp.longitude,
            timezone=resp.timezone,
            location_resolution=resp.location_resolution,
            location_input=resp.location_input,
            current=resp.current,
            source_url=resp.source_url,
            error=None,
        )

    rows = await asyncio.gather(*(_resolve_and_fetch(loc) for loc in locations))
    return ComparisonResponse(
        metric="current",
        locations=rows,
        retrieved_at=datetime.now(timezone.utc),
        server_version=__version__,
    )


@mcp.tool
def list_curated() -> list[str]:
    """List the 45 curated Australian location IDs supported by this MCP.

    The curated set covers all 8 state/territory capitals plus 37 major
    regional centres. Any Australian place name outside this set still
    works via the place-name geocoder; the curated entries just get
    fast-path lookup (no network call) and appear in `search_locations`.

    Coverage by state:
        - 8 capitals: sydney, melbourne, brisbane, perth, adelaide, hobart,
          darwin, canberra
        - NSW regional (10): newcastle, wollongong, tamworth, wagga_wagga,
          albury, orange, bathurst, dubbo, coffs_harbour, port_macquarie
        - VIC regional (6): geelong, ballarat, bendigo, mildura, shepparton,
          warrnambool
        - QLD regional (9): gold_coast, sunshine_coast, cairns, townsville,
          mackay, toowoomba, rockhampton, bundaberg, hervey_bay
        - WA regional (5): broome, bunbury, geraldton, albany, kalgoorlie
        - SA regional (2): mount_gambier, whyalla
        - TAS regional (3): launceston, devonport, burnie
        - NT regional (2): alice_springs, katherine

    When to use:
        - You want to enumerate which locations have first-class support
        - You're building a UI / dashboard that needs the supported set up front
        - You want to plan a multi-location dashboard call

    Returns:
        Sorted list of location IDs (currently 45). Adding a location is
        a YAML edit in src/au_weather_mcp/data/curated/locations.yaml.
    """
    return curated_mod.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
