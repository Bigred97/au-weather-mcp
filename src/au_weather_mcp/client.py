"""Async Open-Meteo client.

Open-Meteo (https://open-meteo.com) is a free weather API that aggregates
many national meteorological services including the Australian Bureau of
Meteorology, ERA5 reanalysis, GFS, and ICON. They handle the licensing
arrangements with each upstream provider; we attribute Open-Meteo + BOM
in every response.

Why Open-Meteo and not BOM direct: BOM blocks non-browser User-Agents and
their endpoints have no SLA, no schema versioning, and no documented
commercial-use path below the ~$5k/yr Registered User Service. Open-Meteo's
free tier explicitly allows non-commercial use; commercial use at scale is
$30/mo. For free-MCP traffic, the free tier is sufficient and clean.

We make two endpoint families:
  - https://api.open-meteo.com/v1/forecast  — current + future weather
  - https://archive-api.open-meteo.com/v1/archive — historical from 1940
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlencode

import httpx

from . import __version__
from .cache import TTL, Cache, CacheKind

FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"
GEOCODING_BASE = "https://geocoding-api.open-meteo.com/v1/search"
AIR_QUALITY_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"
# Nominatim is used as a fallback for AU postcodes (Open-Meteo's geocoder
# returns European cities for 4-digit numerics). Their TOS requires an
# identifying User-Agent and a max of 1 req/sec; cached aggressively.
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ─── stale signal (graceful-degradation reporting per CLAUDE.md dim #4) ─
# When Open-Meteo is unreachable, _fetch falls back to the cached payload
# regardless of TTL and records the staleness in this ContextVar.
# Server-side tool wrappers read it after the request chain and set
# WeatherResponse.stale / .stale_reason. ContextVar (not instance attr) so
# concurrent MCP tool calls each see their own state.
_stale_signal: ContextVar[tuple[bool, str | None]] = ContextVar(
    "au_weather_mcp_stale_signal", default=(False, None)
)


def reset_stale_signal() -> None:
    """Clear the stale state. Call once at the start of each tool call."""
    _stale_signal.set((False, None))


def get_stale_signal() -> tuple[bool, str | None]:
    """Return (stale, reason) for the most recent fetch chain in this context."""
    return _stale_signal.get()


def _mark_stale(reason: str) -> None:
    """Record that a stale-cache fallback was served this context.

    If multiple fetches in one chain are stale, we keep the FIRST reason
    (it's usually the most informative — the originating upstream failure).
    """
    cur_stale, _ = _stale_signal.get()
    if not cur_stale:
        _stale_signal.set((True, reason))


class OpenMeteoError(Exception):
    """Raised when the Open-Meteo API returns a non-2xx response or unparseable body."""


class OpenMeteoClient:
    def __init__(
        self,
        cache: Cache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cache = cache or Cache()
        # Identify honestly. Open-Meteo doesn't block on UA — unlike BOM —
        # but identifying lets them contact us if we ever misbehave.
        # User-Agent carries the actual running version so upstream abuse-
        # triage / rate-limit logs at Open-Meteo or Nominatim point at the
        # exact wheel that made the call. Avoids the "stale 0.1.0 string"
        # drift problem on every patch release.
        self._http = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            headers={
                "User-Agent": (
                    f"au-weather-mcp/{__version__} "
                    "(+https://github.com/Bigred97/au-weather-mcp)"
                ),
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OpenMeteoClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, tb: Any) -> None:
        await self.aclose()

    async def _fetch(self, url: str, kind: CacheKind) -> Any:
        cached = await self.cache.get(url, ttl=TTL[kind])
        if cached is not None:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                # Corrupt cache entry — fall through and re-fetch.
                pass
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # Graceful degradation: when upstream is unreachable, fall back
            # to the most-recent cached payload (regardless of TTL) rather
            # than raising and breaking the agent's chain of reasoning.
            # The staleness is surfaced via the _stale_signal ContextVar
            # and ends up in WeatherResponse.stale / stale_reason.
            # 4xx is treated the same way — Open-Meteo occasionally serves
            # transient 5xx-shaped 4xx errors, and a stale payload is more
            # useful than a hard failure for the agent.
            fallback = await self.cache.get_stale(url)
            if fallback is not None:
                payload_bytes, cached_at = fallback
                try:
                    cached_payload = json.loads(payload_bytes)
                except json.JSONDecodeError:
                    cached_payload = None
                if cached_payload is not None:
                    age_min = max(0, int((time.time() - cached_at) / 60))
                    if isinstance(e, httpx.HTTPStatusError):
                        upstream = f"Open-Meteo returned {e.response.status_code}"
                    else:
                        upstream = f"Open-Meteo unreachable ({type(e).__name__})"
                    _mark_stale(
                        f"{upstream} for {url}; serving cached payload from "
                        f"~{age_min} minute(s) ago"
                    )
                    return cached_payload
            # Genuinely no cache to fall back to — preserve original behaviour
            if isinstance(e, httpx.HTTPStatusError):
                # Open-Meteo returns 4xx with a JSON body describing the error;
                # surface that to the MCP tool so the user gets an actionable hint.
                try:
                    body = e.response.json()
                    reason = body.get("reason", body)
                except Exception:
                    reason = e.response.text[:200]
                raise OpenMeteoError(
                    f"Open-Meteo API returned {e.response.status_code} for {url}: {reason}"
                ) from e
            # Some httpx error classes have an empty str(); surface the type
            # so a concurrency / pool / SSL failure isn't a mystery to debug.
            detail = str(e) or type(e).__name__
            raise OpenMeteoError(f"Open-Meteo API request failed: {detail}") from e
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise OpenMeteoError(f"Failed to parse Open-Meteo JSON from {url}: {e}") from e
        await self.cache.set(url, resp.content, kind=kind)
        return data

    async def forecast(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        current: list[str] | None = None,
        hourly: list[str] | None = None,
        daily: list[str] | None = None,
        forecast_days: int | None = None,
        past_days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        kind: CacheKind = "forecast",
    ) -> dict[str, Any]:
        """Hit the forecast endpoint (current + future weather).

        `start_date` / `end_date` here use YYYY-MM-DD format and only return
        full days. For dates in the past, use `archive()` instead — it has
        deeper history (1940+) and is the authoritative source for past data.
        """
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
        }
        if current:
            params["current"] = ",".join(current)
        if hourly:
            params["hourly"] = ",".join(hourly)
        if daily:
            params["daily"] = ",".join(daily)
        if forecast_days is not None:
            params["forecast_days"] = forecast_days
        if past_days is not None:
            params["past_days"] = past_days
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        url = f"{FORECAST_BASE}?{urlencode(params)}"
        return await self._fetch(url, kind=kind)

    async def air_quality(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        current: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch current air-quality readings (PM2.5, PM10, AQI, etc.) for a
        location. Sourced from Open-Meteo's air-quality API which merges
        Copernicus CAMS European + global air-composition models.

        Cached as 'current' kind — 15-minute TTL matches the upstream
        update cadence.
        """
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
        }
        if current:
            params["current"] = ",".join(current)
        url = f"{AIR_QUALITY_BASE}?{urlencode(params)}"
        return await self._fetch(url, kind="current")

    async def geocode_postcode_au(self, postcode: str) -> dict[str, Any] | None:
        """Resolve a 4-digit Australian postcode to lat/lng + suburb via
        OpenStreetMap's Nominatim service.

        Open-Meteo's geocoder doesn't understand AU postcodes — querying
        '2026' returns Antwerp, Belgium. Nominatim does. We use Nominatim
        only for postcodes (everything else stays on Open-Meteo) to keep
        the rate-limit footprint tiny and respect Nominatim's TOS.

        Returns None if no result. The caller must handle the None case
        (typically by falling through to Open-Meteo's geocoder or raising
        an actionable error).

        Caches as 'metadata' kind (7-day TTL) — postcode boundaries are
        stable on a multi-month timescale.

        Attribution: when the response is used, the calling code must
        surface OpenStreetMap attribution ('© OpenStreetMap contributors,
        ODbL 1.0') in the response envelope.
        """
        params = {
            "postalcode": postcode,
            "country": "Australia",
            "format": "json",
            "limit": 1,
            "addressdetails": 1,
        }
        url = f"{NOMINATIM_BASE}?{urlencode(params)}"
        # Nominatim requires the standard User-Agent header (already set on
        # self._http) but they also recommend distinct identification per
        # tool — we use the package-level UA which carries our GitHub URL.
        try:
            data = await self._fetch(url, kind="metadata")
        except OpenMeteoError:
            # If Nominatim is unreachable/throttled, return None and let the
            # caller decide whether to fall back or surface an error.
            return None
        # Nominatim returns a JSON array, not an object. _fetch parses it as
        # JSON but the cache layer + json.loads handle both shapes fine.
        if isinstance(data, list):
            return data[0] if data else None
        return None

    async def geocode_au(
        self,
        name: str,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Resolve an AU place name to lat/lng via Open-Meteo's geocoding API.

        Returns up to `count` candidates, filtered to country_code='AU' and
        sorted by population (so 'Manly' returns the famous Sydney suburb
        of pop 16k before a tiny QLD locality of unknown pop). Empty list
        if nothing matches.

        The geocoding API is cached as 'metadata' kind (7-day TTL) — place
        coordinates basically never change.
        """
        params = {
            "name": name,
            "count": max(count, 10),  # always pull a wide pool so AU filter has options
            "language": "en",
            "format": "json",
        }
        url = f"{GEOCODING_BASE}?{urlencode(params)}"
        data = await self._fetch(url, kind="metadata")
        results = data.get("results") or []
        # Filter to AU only, then sort by population desc (None pop → 0)
        au_results = [r for r in results if r.get("country_code") == "AU"]
        au_results.sort(key=lambda r: -(r.get("population") or 0))
        return au_results[:count]

    async def archive(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: str,
        end_date: str,
        hourly: list[str] | None = None,
        daily: list[str] | None = None,
    ) -> dict[str, Any]:
        """Hit the historical archive endpoint (1940-onwards).

        The archive runs on a ~5-day lag; queries within the last 5 days
        should use `forecast()` with `past_days` instead.
        """
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            "start_date": start_date,
            "end_date": end_date,
        }
        if hourly:
            params["hourly"] = ",".join(hourly)
        if daily:
            params["daily"] = ",".join(daily)
        url = f"{ARCHIVE_BASE}?{urlencode(params)}"
        return await self._fetch(url, kind="historical")
