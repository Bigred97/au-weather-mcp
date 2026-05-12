# Changelog

## 0.2.0 (2026-05-12)

**Compatibility release** — the `location` parameter now accepts almost
anything a real customer would type. Previous version was restricted to
the 21 curated snake_case IDs; this release adds five new input shapes
behind the same parameter so no agent has to know the curated key format.

- **Accepts six input shapes:**
  1. Curated ID (existing): `"sydney"`, `"gold_coast"`
  2. Curated name in any case, with spaces or hyphens: `"Sydney"`,
     `"SYDNEY"`, `"Gold Coast"`, `"gold-coast"`
  3. State code or full name → returns the capital: `"NSW"`,
     `"Queensland"`, `"Western Australia"`
  4. Raw coordinates: `"-33.87,151.21"` (rejected if outside the AU
     bounding box)
  5. Any AU place name via geocoding: `"Byron Bay"`, `"Margaret River"`,
     `"Toowoomba"`, `"Surfers Paradise"`, `"Coober Pedy"` — uses
     Open-Meteo's geocoding API, filtered to country=AU, sorted by
     population (so ambiguous names like "Newcastle" prefer the NSW city
     over a smaller WA hamlet)
  6. Typos: `"Sydny"`, `"Melbourn"` → high-confidence fuzzy match against
     curated set before falling through to geocode
- **`location_resolution` field on every response** explaining how we got
  there — `"curated"`, `"state_alias"`, `"raw_coordinates"`, `"geocoded"`,
  or `"fuzzy_curated"`. The agent (and the user) can see HOW the input
  was interpreted, which matters when "Manly" is ambiguous (Sydney
  suburb vs Brisbane bayside) or "Bondi" doesn't make Open-Meteo's index.
- **`location_id` is now nullable** on `WeatherResponse` — set to the
  curated key when matched, otherwise None. Backwards-compatible for
  agents that just read `location_name` + lat/lng.
- **Geocoding cached as `metadata` kind** (7-day TTL) — place coordinates
  don't change.
- **+27 regression tests** covering every input shape and customer
  variation, plus 2 live tests exercising real Open-Meteo geocoding for
  Byron Bay and Toowoomba. 89 unit + 10 live tests now.
- **Known limitations** (deferred to v0.3.0): AU postcodes (Open-Meteo's
  geocoder returns European cities for 4-digit numerics — needs a
  separate Nominatim/postcode dataset); reverse geocoding for the
  display name of raw lat/lng inputs (currently shown as `(lat, lng)`).

## 0.1.0 (2026-05-12)

Initial release. Same architecture as abs-mcp / rba-mcp / ato-mcp.

- **5 MCP tools** with Annotated[Field] parameter schemas for Glama-quality
  tool definitions: `search_locations`, `describe_location`, `latest`,
  `get_weather`, `list_curated`.
- **21 curated AU locations** — 8 state/territory capitals + 13 major
  regional centres. Coordinates anchored to canonical BOM observation
  points where possible (Sydney = Observatory Hill, etc.) so values
  cross-check against BOM's own published observations.
- **Open-Meteo backend** rather than direct BOM. BOM 403s non-browser
  User-Agents and has no documented commercial-use path below their
  ~$5k/yr Registered User Service. Open-Meteo aggregates BOM data under
  their licensing arrangement, returns versioned schema-stable JSON, and
  has explicit free + commercial tiers.
- **Trust contract**: every response carries `source_url`, full
  CC-BY 4.0 attribution (Open-Meteo + BOM), `retrieved_at`,
  `server_version`, and a `stale` flag with reason if cached fallback
  is in play. Pydantic sanity validators reject upstream values outside
  the plausible Australian range (temperature, humidity, pressure, wind
  direction).
- **SQLite cache** with per-kind TTLs: current obs 15 min (matches
  Open-Meteo's update cadence), forecasts 1 hour, historical 7 days,
  metadata 7 days. Self-heals if `cache.db` is ever corrupted on disk.
- **URL safety**: location IDs and date strings validated against strict
  regex patterns. Injection characters (?, &, /, #, etc.) rejected at
  the boundary, never sent upstream.
- **Auto-routing in `get_weather`**: end-date in the past >= 5 days routes
  to Open-Meteo's historical archive (1940+); otherwise routes to the
  forecast endpoint (today + 16 days).
- **57 tests**: unit + integration. Unit suite covers curated registry,
  cache (incl. corruption self-heal + concurrency), HTTP client (incl.
  4xx body parsing + cache hit/miss), shaping (column→row pivots,
  sanity validators), and server-level input validation. Live suite
  exercises real Open-Meteo calls for all 8 capitals + historical archive
  + cache warm/cold latency.
