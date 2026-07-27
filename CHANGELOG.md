# Changelog

## [0.4.10] - 2026-07-27

### Fixed — `compare_locations()` silently hid stale-cache fallbacks

`ComparisonResponse` was missing the trust contract carried by every other
response model (`WeatherResponse`, `AirQualityResponse`): `stale`,
`stale_reason`, and `source_url`. When Open-Meteo 5xx'd and a location's
row was served from a stale cached payload, `compare_locations()` returned
a response that looked completely fresh — a silent-staleness trust
violation.

- `models.ComparisonResponse` now declares `source_url` (the shared
  Open-Meteo forecast endpoint), `stale: bool = False`, and
  `stale_reason: str | None = None`, matching the sibling response models.
- `server.compare_locations()` now calls `reset_stale_signal()` at entry
  like every other tool. Because `asyncio.gather` wraps each location's
  fetch in its own `Task` — and each `Task` gets its own copy of the
  `contextvars.Context` — a naive reset/read pairing wrapped only around
  the gather would silently miss staleness raised inside a row's own
  fetch. Each row task now records its own `(stale, reason)` signal after
  its fetch completes; these are folded back into the top-level
  `ComparisonResponse` after the gather finishes, keeping the first
  (most informative) stale reason across all rows.
- Added `test_compare_locations_surfaces_stale_signal` (regression) which
  forces one row's fetch stale and asserts `resp.stale is True` with a
  non-empty `resp.stale_reason`.

## [0.4.9] - 2026-05-21

### Added — MCP Registry ownership marker

- Add `mcp-name: io.ausdata/au-weather-mcp` to the README so the server can be
  listed under the verified `io.ausdata` namespace.

## [0.4.8] - 2026-05-19

### Improved — transport-agnostic error hints + AST guard test

Error messages no longer name MCP-tool-specific functions
(`describe_location(...)`, `list_curated()`). The same `ValueError`
should read cleanly whether the caller is an MCP client, a REST
gateway (ausdata-api), or a Python script calling the functions
directly — naming an MCP tool by-name leaks transport details that
half the callers can't act on.

Three error-message sites rewritten:
- `resolution._try_lat_lng` — coord-out-of-AU hint
- `resolution._try_postcode` — non-mainland-postcode hint
- `resolution.resolve_location` — non-string input hint
- `server.latest` — current-weather fetch failure
- `server.get_weather` — historical-range fetch failure
- `server.air_quality` — air-quality fetch failure

### Added — `test_no_mcp_tool_refs_in_error_strings`

AST-based guard test (mirrors abs-mcp / rba-mcp's validation
pattern). Walks every `.py` under `src/au_weather_mcp/`, parses
each `raise <SomeExc>(...)` call, and asserts the string argument
doesn't match `describe_location|search_locations|list_curated`
followed by an open paren. Locks in the no-tool-name-leakage rule
going forward — any future regression fails CI before merge.

111 unit tests pass (+1 guard test).

## [0.4.7] - 2026-05-16

### Fixed
- Underscore place-name resolution regression: `margaret_river`,
  `byron_bay` etc. now resolve identically to their spaced
  equivalents (`Margaret River`, `Byron Bay`). Both forms are
  accepted as documented.

## 0.4.6 (2026-05-15)

**Error-message sweep — rejection messages now suggest the correction.**
Quality dimension #5 in CLAUDE.md (Deterministic Error Handling): every
`ValueError` must carry a "Try X" / "Valid options:" / "Did you mean X?"
hint, not just describe the rejection. Audit found 11 raise sites whose
messages only described what went wrong without telling the caller how
to fix it; this release rewrites all 11.

- **`models.py` sanity validators (5 sites)** — `temperature_c`,
  `relative_humidity_pct`, `pressure_msl_hpa`, `wind_direction_deg`,
  `DailyAggregate` temperatures. These trip on upstream Open-Meteo / BOM
  data anomalies (not caller errors), so the new messages route the
  user to retry-after-cache-TTL and to the GitHub issue tracker.
- **`server.py` (4 sites)** — `search_locations` limit validation now
  suggests `limit=10` (the default); `get_weather` granularity rejection
  lists both valid options and suggests `'daily'`; `get_weather` /
  `air_quality` fetch-error paths now point at `describe_location()` to
  verify resolved coordinates; `compare_locations` non-list rejection
  now shows a concrete `['sydney', 'melbourne']` example.
- **`resolution.py` (2 sites)** — `location` non-string rejection now
  lists all five accepted input shapes with examples; lat/lng bbox
  rejection suggests valid AU coordinates and curated alternatives.
  Postcode-outside-bbox rejection now points at mainland postcodes
  ('2000', '3000', '4000') and `list_curated()`.
- **`curated.py` (1 site, developer-facing)** — YAML loader's
  snake_case rejection now shows example IDs (`'gold_coast'`,
  `'alice_springs'`, `'mount_gambier'`).

No behavioural change — same conditions raise the same exception type
(`ValueError`); only the message strings improved. Tool surface, the
5-tool contract, response envelope, and cache layer are all unchanged.

- **+2 regression tests** in `test_server_validation.py`:
  1. `granularity` error must list valid options AND carry a "Try X" pointer
  2. `compare_locations` non-list error must show a concrete example call
- 108 unit tests now (was 106 in 0.4.5). 20 live tests unchanged.
  10×10 zero-flake confirmed before tagging.

## 0.4.5 (2026-05-15)

Graceful degradation — quality dimension #4 in CLAUDE.md. Propagated from
the abs-mcp 0.2.13 reference implementation.

When Open-Meteo is unreachable (5xx, timeout, DNS failure, connection
refused), the client now falls back to the most-recent cached payload
regardless of TTL and surfaces the staleness in the response. Agents see
`WeatherResponse.stale=True` with a `stale_reason` like *"Open-Meteo
unreachable (ConnectError) for https://...; serving cached payload from
~17 minute(s) ago"* and can continue reasoning, rather than the tool
raising and breaking the chat.

Genuine no-cache-to-fall-back-to case still raises `OpenMeteoError` —
only degrade gracefully when there's something to degrade to.

- **New: `Cache.get_stale(key) -> (payload, cached_at)`** — TTL-bypassing
  read, the building block for the fallback path.
- **New: `_stale_signal` ContextVar in `client.py`** — `reset_stale_signal()`
  + `get_stale_signal()` are the public API. The server resets at the
  start of each tool call and reads at the end to propagate `stale=True`
  into the response. Tool methods wired: `latest`, `get_weather`,
  `air_quality`. `WeatherResponse.stale` / `stale_reason` already
  existed on the model.
- **+4 regression tests** in `test_client.py`:
  1. 503 + stale cache → fallback + stale flag set
  2. ConnectError + stale cache → same
  3. 503 + empty cache → raises `OpenMeteoError` (unchanged behaviour)
  4. `Cache.get_stale()` round-trip + TTL bypass verification
- 106 unit tests now (was 102 in 0.4.4). 20 live tests unchanged.

## 0.4.4 (2026-05-13)

**First PyPI release via Trusted Publishing (OIDC).** No code changes vs
0.4.3 — this release exists to fire the `release.yml` workflow (which was
added 41 minutes after `v0.4.3` was tagged) and publish the wheel to PyPI
for the first time using PyPI's OpenID Connect trusted-publisher flow.
No long-lived API tokens in the repo. 102 unit + 20 live tests still green.

Install: `uvx --upgrade au-weather-mcp`.

## 0.4.3 (2026-05-13)

**Iter-3 cleanup.** Adversarial probe 5/5 PASS, trust audit 3/3 PASS,
code review found one remaining doc-drift: `curated.py`'s module
docstring still said "21 AU places" — same class of drift v0.4.2
fixed in `list_curated()`'s docstring, just missed at the module
level. Now says 45.

No code or behaviour change. 102 unit + 20 live tests still green.

## 0.4.2 (2026-05-13)

**Iter-2 audit cleanups.** Agent C PASS, Agent A 0 bugs, Agent B and
Agent A's bonus pass found two consistency items.

- **Fix: `latest()` + `get_weather()` source_url round-trip.** v0.4.1
  fixed the urlencode-vs-raw-commas asymmetry for `air_quality` and
  `compare_locations` but left `latest()` and `get_weather()` building
  source_url with raw commas (`,`) where the client uses `urlencode()`
  (percent-encoded `%2C`). Symmetric fix now applied — all four
  weather-call paths advertise byte-identical URLs to what was served.
- **Doc fix: `search_locations` docstring.** Said "Fuzzy-search the
  21 curated Australian locations" — stale since v0.4.0 doubled to
  45. Now matches the YAML and `list_curated()`'s docstring.

No behavioural change for callers. 102 unit + 20 live = 122 tests
still green.

## 0.4.1 (2026-05-13)

**Iter-1 audit fixes** on v0.4.0. Agent A (adversarial) found 0 bugs,
Agent C (trust) PASS, Agent B (code review) flagged 4 real items — all
addressed here.

- **Fix: `compare_locations` row isolation.** The fan-out previously
  only caught `ValueError` (from resolve) and `OpenMeteoError` (from
  fetch). A `pydantic.ValidationError` from a sanity check tripping on
  upstream bad data, or any `httpx` error not wrapped as
  `OpenMeteoError`, would crash the whole `asyncio.gather` and poison
  every sibling row — defeating the documented "one row failing must
  not poison others" contract. Now: broad `except Exception` barrier
  around both stages of `_resolve_and_fetch` turns any failure into a
  per-row `error` field. Regression test simulates an `httpx`-level
  `RuntimeError` mid-gather; the other rows still succeed.
- **Fix: `source_url` round-trip fidelity (air_quality).** The
  `source_url` advertised on `AirQualityResponse` was built with raw
  commas in `current=pm10,pm2_5,…`, but `client.air_quality()` uses
  `urlencode()` which percent-encodes commas. The advertised URL was
  not byte-identical to the URL Open-Meteo actually served — a
  citation footgun. Now built via the same `urlencode()` call.
- **Fix: `source_url` round-trip fidelity (compare_locations rows).**
  Same pattern inside the fan-out: row `source_url` now built via
  `urlencode()` to match `client.forecast()`.
- **Cleanup: `shaping.build_air_quality_response`.** Removed
  contradictory `current = payload.get("current") or {}` then
  `if current else None` — the `or {}` masked an `observation = None`
  fall-through. Cleaner: gate on `payload.get("current")` directly,
  surface `None` cleanly when upstream returns no current block.
- **Hardening: `__init__.py` against `_version() → None`.** Stale
  editable-install dist-info can cause `importlib.metadata.version()`
  to return `None` instead of raising `PackageNotFoundError`, leaving
  `__version__ = None` and breaking every `server_version` field
  (Pydantic `str` validation fails). Added `or "0.0.0+unknown"`
  fallback so `__version__` is always a non-empty string.
- **+2 regression tests** locking in the row-isolation fix and the
  source_url urlencode round-trip. 102 unit + 20 live = 122 total,
  all green.

## 0.4.0 (2026-05-13)

**Coverage + capability expansion.** Doubles the curated set, adds an
air-quality endpoint, and adds a multi-location comparison tool. All
three are net-additive — no behavioural change for existing callers.

- **21 → 45 curated locations.** 24 new regional centres covering every
  AU population centre over ~25k. By state:
  - NSW (+8): Tamworth, Wagga Wagga, Albury, Orange, Bathurst, Dubbo,
    Coffs Harbour, Port Macquarie
  - VIC (+3): Mildura, Shepparton, Warrnambool
  - QLD (+4): Toowoomba (pop 135k), Rockhampton, Bundaberg, Hervey Bay
  - WA (+4): Bunbury, Geraldton, Albany, Kalgoorlie
  - SA (+2): Mount Gambier, Whyalla
  - TAS (+2): Devonport, Burnie
  - NT (+1): Katherine

  Curated entries get fast-path lookup (no network) and appear in
  `search_locations` / `list_curated`. Anything outside the 45 still
  works via the place-name geocoder or postcode lookup.

- **New tool: `air_quality(location)`** — current PM2.5, PM10, ozone,
  NO₂, SO₂, CO concentrations in µg/m³, plus European AQI + US AQI
  with plain-English labels ("Good", "Moderate", "Unhealthy", etc.).
  Sourced from Open-Meteo's air-quality API (Copernicus CAMS merge).
  Especially relevant during AU bushfire season (Oct–Mar) when smoke
  can push PM2.5 above safe levels across whole regions. Same trust
  contract as `latest()` — `source_url`, full attribution, server
  version. OSM attribution surfaces when location is postcode-resolved.

- **New tool: `compare_locations(locations)`** — side-by-side current
  weather for 2–10 Australian locations in a single call. Fans out
  concurrently via `asyncio.gather`, so all locations come back in
  ~the time of a single fetch (after cache warm-up). Mixed input
  shapes work: `["sydney","NSW","2026","-33.87,151.21"]` resolves
  via curated/state/postcode/lat-lng respectively, all in one
  response. A single bad input surfaces as an `error` field on its
  own row without taking down the whole call.

- **Diagnostic fix**: `Open-Meteo API request failed: <empty>` errors
  no longer have empty detail when httpx raises an exception with
  no string repr (e.g. some `RemoteProtocolError` variants). The
  error type name is now surfaced.

- **+9 regression tests** (4 unit + 5 live). 100 unit + 20 live = 120
  total, all green.

## 0.3.4 (2026-05-12)

**Iter-4 docs hygiene** — closes the last item found by the
adversarial probe. Same class of bug as 0.3.3's UA fix, just in the
docs: the README's example response object hardcoded
`"server_version": "0.1.0"` while the runtime correctly emitted
`"0.3.3"`. Customers copy-pasting the example would have had an
inaccurate expectation.

- README example response now uses a placeholder
  `"server_version": "<package version, e.g. 0.3.3>"` so the
  literal doesn't drift again on future patch releases.
- Same example response now also shows the `location_resolution` and
  `location_input` fields (added in 0.2.0) that were missing from
  the original 0.1.0 example.

No code changes. 111 tests still green.

## 0.3.3 (2026-05-12)

**Iter-3 audit hygiene fix** — closes the last item flagged by the code-
review agent. Adversarial probe and trust audit both clean.

- Client User-Agent now reads `__version__` from `importlib.metadata`
  instead of a hardcoded string. The previous `au-weather-mcp/0.1.0`
  literal drifted on every release; upstream abuse-triage or
  rate-limit alerts at Open-Meteo / Nominatim would point at code
  that no longer exists. Now: every HTTP request to either provider
  carries the exact wheel version that made the call.

No behavioural change for customers. 111 tests still green.

## 0.3.2 (2026-05-12)

**Iter-2 audit micro-refactors** — zero customer-visible bugs surfaced
(Agent A and C both PASS on 0.3.1), this release just absorbs four
small efficiency / hygiene items flagged by the code-review agent.

- `_get_client()` now uses the double-checked-locking pattern from
  `cache.py` — fast-paths every tool call after first init instead of
  taking the lock on every invocation.
- `from rapidfuzz import fuzz` hoisted out of `_try_fuzzy_curated`'s
  hot path to module scope. Saves a per-call re-import.
- Standard `__aexit__(self, exc_type, exc_val, tb)` signature on the
  client. Was `*exc: Any` — works, but obscures the contract.
- Dropped redundant `if daily_obs else None` ternaries inside the
  `if daily_obs:` block in `shaping.build_response` (unreachable —
  the outer guard already established truthiness). Same for hourly.

No behavioural change. 111 tests still green. Agent A and C audits
remain clean; this clears the last of Agent B's iter-2 backlog so
the loop can converge.

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
