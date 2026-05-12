"""Location-input resolution — the compatibility layer.

A customer can pass a `location` parameter in any of the following shapes:

  1. Curated ID (snake_case)        →  "sydney", "gold_coast"
  2. Curated name (any case + space)→  "Sydney", "GOLD COAST", "gold coast"
  3. State code                     →  "NSW", "VIC", "WA" (returns the capital)
  4. State name                     →  "New South Wales", "Victoria"
  5. Raw coordinates                →  "-33.87,151.21" or "-33.87, 151.21"
  6. AU place name (geocoded)       →  "Bondi", "Margaret River", "Wagga Wagga"
  7. Fuzzy-typo of a curated id     →  "Sydny" → suggests / resolves to sydney

We try them in order — fast paths first, network paths last. Each successful
resolution sets a `source` field on the ResolvedLocation so the response
envelope is honest about how we got the coordinates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import curated as curated_mod
from .curated import CuratedLocation

# Lat/lng pattern: optional leading minus, digits, optional decimal, comma, same again.
# AU bounding box check happens after parsing.
_LAT_LNG_PATTERN = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)
# AU postcode: exactly 4 digits. Leading zeros valid (NT/ACT use 0xxx and 2xxx
# respectively). Range is 0200-9999 in practice but we don't validate the
# range — Nominatim is the authority and will return no result for invalid
# postcodes.
_POSTCODE_PATTERN = re.compile(r"^\d{4}$")

# Australia bounding box (generous — covers all states + territorial waters)
AU_LAT_MIN, AU_LAT_MAX = -45.0, -9.0
AU_LNG_MIN, AU_LNG_MAX = 110.0, 156.0

# Map of state codes (and full state names) → curated capital location id.
# Same map handles "NSW" / "nsw" / "New South Wales" / "new south wales".
STATE_TO_CAPITAL: dict[str, str] = {
    # 8 state/territory codes
    "NSW": "sydney",
    "VIC": "melbourne",
    "QLD": "brisbane",
    "WA": "perth",
    "SA": "adelaide",
    "TAS": "hobart",
    "NT": "darwin",
    "ACT": "canberra",
    # Full names
    "NEW SOUTH WALES": "sydney",
    "VICTORIA": "melbourne",
    "QUEENSLAND": "brisbane",
    "WESTERN AUSTRALIA": "perth",
    "SOUTH AUSTRALIA": "adelaide",
    "TASMANIA": "hobart",
    "NORTHERN TERRITORY": "darwin",
    "AUSTRALIAN CAPITAL TERRITORY": "canberra",
    # Country-level (no national aggregate from Open-Meteo, default to the most-asked)
    "AUSTRALIA": "sydney",
    "AU": "sydney",
}

# State / territory → IANA timezone. Authoritative for postcode resolutions
# where we know the state. NT and SA both use Australian Central Standard
# Time (UTC+9:30) but observe DST differently — SA does, NT doesn't.
_STATE_TO_TZ: dict[str, str] = {
    "New South Wales": "Australia/Sydney",
    "Victoria": "Australia/Melbourne",
    "Queensland": "Australia/Brisbane",
    "Western Australia": "Australia/Perth",
    "South Australia": "Australia/Adelaide",
    "Tasmania": "Australia/Hobart",
    "Northern Territory": "Australia/Darwin",
    "Australian Capital Territory": "Australia/Sydney",
}

# Rough longitude-based timezone fallback for raw lat/lng input when we
# don't know the state. Open-Meteo accepts 'auto' which is what we actually
# use for the weather call — this map is only for the response metadata.
# NOTE: imprecise at QLD/NSW border (both around 153°E). If you have a
# state name, prefer _STATE_TO_TZ.
_LON_TO_TZ = [
    (129.0, "Australia/Perth"),    # WA
    (138.5, "Australia/Darwin"),   # NT (no DST)
    (141.0, "Australia/Adelaide"), # SA
    (180.0, "Australia/Sydney"),   # most-asked east-coast zone
]


@dataclass(frozen=True)
class ResolvedLocation:
    """The outcome of resolving a customer-supplied location string.

    For curated lookups, `curated_id` is set and matches a key from
    curated.list_ids(). For non-curated lookups, `curated_id` is None and
    `name` / `state` / `latitude` / `longitude` come from the resolution
    source.

    `source` values:
      - "curated"          — exact or normalised match against curated YAML
      - "state_alias"      — state code / name resolved to a capital
      - "raw_coordinates"  — user supplied "lat,lng"
      - "postcode"         — 4-digit AU postcode resolved via OSM Nominatim
      - "geocoded"         — Open-Meteo geocoding API
      - "fuzzy_curated"    — typo or near-miss resolved against curated set
    """
    name: str
    state: str | None
    country: str
    latitude: float
    longitude: float
    timezone: str
    elevation_m: float | None
    curated_id: str | None
    source: str  # see docstring
    original_input: str


def _longitude_to_tz(lng: float) -> str:
    """Rough Australia-only timezone fallback used when no geocoded TZ is
    available. Real API calls use timezone='auto' anyway."""
    for cutoff, tz in _LON_TO_TZ:
        if lng < cutoff:
            return tz
    return "Australia/Sydney"


def _normalize_to_curated_key(s: str) -> str:
    """Turn 'Gold Coast', 'GOLD COAST', 'gold-coast' → 'gold_coast'.

    Used as a fast pre-lookup before the curated registry check; gives
    case- and whitespace-insensitive matching for free.
    """
    s = s.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s


def _from_curated(loc: CuratedLocation, *, source: str, original_input: str) -> ResolvedLocation:
    return ResolvedLocation(
        name=loc.name,
        state=loc.state,
        country="Australia",
        latitude=loc.latitude,
        longitude=loc.longitude,
        timezone=loc.timezone,
        elevation_m=loc.elevation_m,
        curated_id=loc.id,
        source=source,
        original_input=original_input,
    )


def _try_lat_lng(s: str, original: str) -> ResolvedLocation | None:
    """If `s` looks like 'lat,lng', parse + validate against AU bbox."""
    m = _LAT_LNG_PATTERN.match(s)
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lng = float(m.group(2))
    except ValueError:
        return None
    if not (AU_LAT_MIN <= lat <= AU_LAT_MAX and AU_LNG_MIN <= lng <= AU_LNG_MAX):
        # Refuse to silently serve data for coords outside the AU bbox —
        # that would be a misleading scope expansion.
        raise ValueError(
            f"coordinates ({lat}, {lng}) are outside the Australia bounding box "
            f"({AU_LAT_MIN}..{AU_LAT_MAX} lat, {AU_LNG_MIN}..{AU_LNG_MAX} lng). "
            "au-weather-mcp only serves Australian locations."
        )
    return ResolvedLocation(
        name=f"({lat:.4f}, {lng:.4f})",
        state=None,
        country="Australia",
        latitude=lat,
        longitude=lng,
        timezone=_longitude_to_tz(lng),
        elevation_m=None,
        curated_id=None,
        source="raw_coordinates",
        original_input=original,
    )


def _try_state(s: str, original: str) -> ResolvedLocation | None:
    """If `s` is a state code or full state name, return the capital."""
    cap_id = STATE_TO_CAPITAL.get(s.strip().upper())
    if cap_id is None:
        return None
    loc = curated_mod.get(cap_id)
    if loc is None:
        return None  # registry corrupted; let the next stage handle
    return _from_curated(loc, source="state_alias", original_input=original)


def _try_curated_exact_or_normalised(s: str, original: str) -> ResolvedLocation | None:
    """First try the literal string against curated.get(), then a normalised
    snake_case form. This is the case-insensitive + whitespace-tolerant path."""
    direct = curated_mod.get(s)
    if direct is not None:
        return _from_curated(direct, source="curated", original_input=original)
    normalised = _normalize_to_curated_key(s)
    if normalised and normalised != s.strip().lower():
        loc = curated_mod.get(normalised)
        if loc is not None:
            return _from_curated(loc, source="curated", original_input=original)
    return None


def _try_fuzzy_curated(s: str, original: str, min_score: float = 85.0) -> ResolvedLocation | None:
    """High-confidence fuzzy match against curated names (catches typos like
    'Sydny' → 'sydney'). Only triggers above `min_score` to avoid silently
    resolving genuinely-different places to a curated location."""
    matches = curated_mod.search(s, limit=1)
    if not matches:
        return None
    # Re-score the top match explicitly using rapidfuzz to apply the threshold
    from rapidfuzz import fuzz
    top = matches[0]
    candidates = [top.id, top.name, f"{top.name} {top.state}"]
    best = max(fuzz.WRatio(s.lower(), c.lower()) for c in candidates)
    if best < min_score:
        return None
    return _from_curated(top, source="fuzzy_curated", original_input=original)


def _parse_state_from_display_name(display_name: str) -> str | None:
    """Nominatim returns 'display_name' like
    '2026, Bondi Beach, Waverley Council, New South Wales, 2026, Australia'.
    The state is the second-to-last comma-separated token before 'Australia'.
    Returns None if we can't parse it confidently."""
    if not display_name:
        return None
    parts = [p.strip() for p in display_name.split(",")]
    # Walk from the end: skip 'Australia' and any trailing postcode token,
    # the next non-trivial token is the state.
    au_states = {
        "New South Wales", "Victoria", "Queensland", "Western Australia",
        "South Australia", "Tasmania", "Northern Territory",
        "Australian Capital Territory",
    }
    for p in reversed(parts):
        if p in au_states:
            return p
    return None


def _parse_suburb_from_nominatim(entry: dict) -> str:
    """Extract a useful place name from a Nominatim entry. Prefers structured
    address.suburb / town / city, falls back to the first display_name token
    (which for postcode queries is usually 'Suburb Name')."""
    addr = entry.get("address") or {}
    for key in ("suburb", "town", "city", "village", "hamlet", "locality"):
        if addr.get(key):
            return str(addr[key])
    display = entry.get("display_name") or ""
    if display:
        parts = [p.strip() for p in display.split(",")]
        # First token is often the postcode itself; second token is the suburb.
        if len(parts) >= 2 and parts[0].isdigit():
            return parts[1]
        if parts:
            return parts[0]
    return "Unknown"


async def _try_postcode(client, s: str, original: str) -> ResolvedLocation | None:
    """If `s` is a 4-digit AU postcode, resolve via OSM Nominatim. Returns
    None if not a postcode shape OR if Nominatim returns no result (caller
    falls through to the Open-Meteo geocoder)."""
    if not _POSTCODE_PATTERN.match(s):
        return None
    entry = await client.geocode_postcode_au(s)
    if not entry:
        return None
    try:
        lat = float(entry["lat"])
        lng = float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    name = _parse_suburb_from_nominatim(entry)
    state = entry.get("address", {}).get("state") or _parse_state_from_display_name(
        entry.get("display_name") or ""
    )
    # Prefer state-based timezone (authoritative) over the longitude
    # heuristic, which is wrong on the QLD/NSW border.
    tz = _STATE_TO_TZ.get(state) if state else None
    if tz is None:
        tz = _longitude_to_tz(lng)
    return ResolvedLocation(
        name=name,
        state=state,
        country="Australia",
        latitude=lat,
        longitude=lng,
        timezone=tz,
        elevation_m=None,  # Nominatim doesn't return elevation
        curated_id=None,
        source="postcode",
        original_input=original,
    )


async def resolve_location(client, location_input: str) -> ResolvedLocation:
    """Main entry: resolve any reasonable customer input to a ResolvedLocation.

    Tries the stages in this order (fast → expensive):

      1. raw "lat,lng" coordinates
      2. exact curated id or normalised case-folded form
      3. state code or full state name → capital
      4. 4-digit AU postcode via OSM Nominatim
      5. high-confidence fuzzy match against curated set (catches typos)
      6. Open-Meteo geocoding API (filtered to AU, sorted by population)

    Raises ValueError with an actionable hint if nothing matches.
    """
    if not isinstance(location_input, str):
        raise ValueError(
            f"location must be a string, got {type(location_input).__name__}."
        )
    s = location_input.strip()
    if not s:
        raise ValueError(
            "location is empty. Try a curated ID like 'sydney', a state code "
            "like 'NSW', a postcode like '2026', a place name like 'Bondi Beach', "
            "or coordinates like '-33.87,151.21'."
        )

    # 1. lat/lng
    coords = _try_lat_lng(s, original=location_input)
    if coords is not None:
        return coords

    # 2. exact curated / normalised
    cur = _try_curated_exact_or_normalised(s, original=location_input)
    if cur is not None:
        return cur

    # 3. state code / state name
    state = _try_state(s, original=location_input)
    if state is not None:
        return state

    # 4. AU postcode via Nominatim
    postcode = await _try_postcode(client, s, original=location_input)
    if postcode is not None:
        return postcode

    # 5. high-confidence fuzzy curated (catches Sydny → sydney, with score gate)
    fuzzy = _try_fuzzy_curated(s, original=location_input)
    if fuzzy is not None:
        return fuzzy

    # 6. geocode via Open-Meteo (AU-filtered, population-sorted)
    candidates = await client.geocode_au(s, count=5)
    if candidates:
        best = candidates[0]
        return ResolvedLocation(
            name=str(best.get("name", s)),
            state=best.get("admin1"),
            country="Australia",
            latitude=float(best["latitude"]),
            longitude=float(best["longitude"]),
            timezone=str(best.get("timezone") or _longitude_to_tz(float(best["longitude"]))),
            elevation_m=(float(best["elevation"]) if best.get("elevation") is not None else None),
            curated_id=None,
            source="geocoded",
            original_input=location_input,
        )

    # Nothing matched. Suggest curated alternatives via fuzz (lower threshold).
    suggestions = [m.id for m in curated_mod.search(s, limit=5)]
    raise ValueError(
        f"Could not resolve location {location_input!r}. "
        f"Tried curated set, state codes, postcodes, coordinates, and geocoding. "
        f"Did you mean: {', '.join(suggestions)}? "
        "Other accepted shapes: 'NSW' (state code), '2026' (AU postcode), "
        "'-33.87,151.21' (coordinates), or a place name like 'Margaret River'."
    )
