# Changelog

## 0.3.1 (2026-05-12)

**Iter-1 audit fixes** — two real bugs surfaced by an adversarial probe
against 0.3.0 + four quality refactors. Code-review audit (Agent B) and
trust audit (Agent C) confirmed no other issues; this is a focused
patch release, not a rewrite.

- **Fix: `describe_location` crashed on every non-curated input.**
  `LocationDetail.id` was non-nullable (`str`) but the server passed
  `id=resolved.curated_id` which is `None` for postcode, raw-coords,
  geocoded, and fuzzy-curated paths. Every such call raised
  `pydantic_core.ValidationError` — a leaked exception type, not the
  documented `ValueError` contract. Now `id: str | None`, matching
  `WeatherResponse.location_id`'s nullable semantics. 8 distinct
  customer-typed inputs that reproduced this in iter-1 audit
  (Margaret River, 2026, 4870, 3000, raw coords, Wagga Wagga, etc.)
  now all return `LocationDetail` with `id=None` and full provenance.
- **Fix: Norfolk Island postcode 2899 bypassed the AU bbox guard.**
  Nominatim resolves 2899 to (−29.04, 167.95) — longitude 167.95 is
  outside the declared AU bbox (110–156). An equivalent raw-coord
  input is correctly rejected, but the postcode path was silently
  serving weather for an out-of-scope location. The bbox check now
  runs inside `_try_postcode` too. Norfolk Island, Christmas Island
  (6798), and Cocos Islands (6799) postcodes now error cleanly:
  *"…outside the Australia bounding box; au-weather-mcp covers the
  AU mainland and Tasmania; external territories…not in scope."*
- **`location_resolution` is now `Literal[…]`** instead of bare `str`,
  including the previously-missing `"postcode"` value. Adding a new
  resolution path requires updating the type — Pydantic will reject
  any unlisted value, catching the "forgot to update the type"
  mistake at write time rather than ship time.
- **Single source of truth for attribution strings.** `DEFAULT_ATTRIBUTION`
  and `OSM_ATTRIBUTION_SUFFIX` constants in `models.py`, imported
  everywhere. The full string was duplicated in three places before
  and would drift if licensing wording ever changed.
- **Cleanup**: collapsed identical `_DEFAULT_CURRENT_VARS` /
  `_DEFAULT_HOURLY_VARS` lists; dropped unused exception binding
  in the Nominatim fallback path.
- **+4 regression tests** locking in both bug fixes. 96 unit + 15 live
  = 111 total. All green.

## 0.3.0 (2026-05-12)

**Postcode support** — 4-digit AU postcodes now resolve via OpenStreetMap
Nominatim. Closes the gap flagged in 0.2.0's "known limitations" — agents
can now answer "what's the weather at 2026?" with Bondi Beach data.

- **AU postcode resolution stage** sits between the fuzzy-curated check
  and Open-Meteo geocoding. Triggered when input matches `^\d{4}$`.
  Uses Nominatim's structured `postalcode=` query (filtered to
  `country=Australia`) to get the canonical suburb + lat/lng.
- **Why Nominatim and not Open-Meteo's geocoder for postcodes?**
  Open-Meteo's geocoder returns European cities for AU 4-digit numerics
  ('2000' → Antwerp, '3000' → Bern). Nominatim handles postcodes
  correctly across countries. We use it ONLY for postcodes to keep the
  Nominatim request footprint minimal and respect their 1 req/sec TOS.
- **State-based timezone** for postcode-resolved locations. The
  longitude-only heuristic was wrong at the QLD/NSW border (both around
  153°E). Now: state → IANA timezone map for known states, longitude
  heuristic only as ultimate fallback for raw lat/lng.
- **OSM attribution baked into the response when Nominatim was used**.
  Nominatim's ODbL licence requires attribution to travel with the data.
  When `location_resolution == 'postcode'`, the response's `attribution`
  field appends "© OpenStreetMap contributors, ODbL — https://www.openstreetmap.org/copyright".
  Non-postcode responses are unchanged (still Open-Meteo + BOM only).
- **Cache TTL**: 7 days (`metadata` kind). Postcode boundaries are stable.
- **`postcode`** added to the `location_resolution` source enum.

Customer-typed inputs that now resolve:

| Input | Resolves to |
|---|---|
| `"2026"` | Bondi Beach, NSW |
| `"3000"` | Melbourne, VIC |
| `"4217"` | Southport (Gold Coast), QLD |
| `"6160"` | Fremantle, WA |
| `"7000"` | Hobart, TAS |
| `"0800"` | Darwin, NT |

- **+7 regression tests** for postcode resolution (5 unit + 2 live).
  Total: 96 unit + 11 live = 107 tests. Live test for Bondi Beach (2026)
  passes end-to-end against the real Nominatim API.

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
