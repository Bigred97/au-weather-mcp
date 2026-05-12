# au-weather-mcp

[![tests](https://github.com/Bigred97/au-weather-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Bigred97/au-weather-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/au-weather-mcp.svg)](https://pypi.org/project/au-weather-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/au-weather-mcp.svg)](https://pypi.org/project/au-weather-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Ask Claude about Australian weather and get real, current numbers** — not "I don't have access to that data." This MCP server gives Claude (and other MCP clients like Cursor) live access to Australian weather data via [Open-Meteo](https://open-meteo.com), which aggregates Bureau of Meteorology observations under licence. Plain-English location keys (`sydney`, `cairns`, `alice_springs`), 80+ years of historical data, 16-day forecasts.

Companion to [abs-mcp](https://github.com/Bigred97/abs-mcp) (ABS macro stats), [rba-mcp](https://github.com/Bigred97/rba-mcp) (Reserve Bank), and [ato-mcp](https://github.com/Bigred97/ato-mcp) (tax + charity register) — together the four cover Australia's most-asked public data.

## What you can ask

Once installed, your LLM can answer questions like:

| Question | Real response |
|---|---|
| What's the weather in Sydney right now? | Current temperature, humidity, wind, rain, pressure with the time stamped |
| Forecast for Melbourne next week? | 7-day daily forecast with max/min temps and rain |
| How was Sydney summer in January 2020? | Historical daily data from Open-Meteo's archive (1940+) |
| Compare rainfall in Cairns vs Brisbane this year | Multi-location queries with provenance per row |
| Tropical Queensland weather today | Search fuzzy by region/state/description |

Every response carries a CC-BY 4.0 attribution string and a direct Open-Meteo URL the agent can cite back to the user.

## Why Open-Meteo (not BOM directly)

BOM publishes their own JSON/XML endpoints, but they actively 403 non-browser User-Agents and have no documented commercial-use path below their ~$5k/yr Registered User Service. Open-Meteo:

- Aggregates BOM data under their existing licensing arrangements with national meteorological services
- Free tier is explicit and generous; commercial use is $30/mo with public terms
- Returns clean, versioned, schema-stable JSON with units alongside every value
- Covers historical data back to 1940 via their archive endpoint
- No API key, no User-Agent gymnastics

We attribute both Open-Meteo and BOM in every response.

## Install

```bash
# After publish:
uvx au-weather-mcp

# Local dev:
uv pip install -e .
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "weather": {
      "command": "uvx",
      "args": ["--upgrade", "au-weather-mcp"]
    }
  }
}
```

The `--upgrade` flag makes uvx re-check PyPI on each Claude Desktop launch, so bug fixes propagate without manual cache refresh. Costs ~100ms at startup.

For a local checkout (before PyPI publish):

```json
{
  "mcpServers": {
    "weather": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/au-weather-mcp", "au-weather-mcp"]
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "weather": {
      "command": "uvx",
      "args": ["--upgrade", "au-weather-mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `search_locations(query, limit=10)` | Fuzzy-search the 21 curated AU locations by name, state, or description. |
| `describe_location(location)` | Lat/lng, timezone, elevation, nearest BOM station, and the canonical Open-Meteo URL. |
| `latest(location)` | Current weather observation — temp, humidity, wind, rain, pressure. 15-min cache. |
| `get_weather(location, start_date, end_date, granularity)` | Time-series query. Auto-routes to historical archive (1940+) or forecast (today + 16 days). Daily or hourly granularity. |
| `list_curated()` | All 21 supported location IDs. |

## Accepts almost any input shape

The `location` parameter on every tool resolves six different input shapes — agents and users don't need to know the curated key format:

| Input shape | Example | Resolves via |
|---|---|---|
| Curated ID | `"sydney"`, `"gold_coast"` | Direct curated lookup (fast) |
| Place name, any case | `"Sydney"`, `"Gold Coast"`, `"GOLD COAST"` | Normalised curated lookup |
| State code or full name | `"NSW"`, `"Queensland"`, `"Western Australia"` | State → capital alias |
| Raw coordinates | `"-33.87,151.21"` | Direct lat/lng (AU bbox enforced) |
| **AU postcode** | `"2026"` (Bondi Beach), `"4217"` (Gold Coast), `"6160"` (Fremantle) | **OpenStreetMap Nominatim** |
| Any AU place name | `"Byron Bay"`, `"Margaret River"`, `"Toowoomba"` | Open-Meteo geocoding (AU-filtered, population-sorted) |
| Typo of a curated name | `"Sydny"`, `"Melbourn"` | High-confidence fuzzy match |

Every response includes a `location_resolution` field with one of `curated`, `state_alias`, `raw_coordinates`, `geocoded`, or `fuzzy_curated` — so the agent (and the user) can see HOW the input was interpreted.

## Curated locations

21 locations covering all 8 state/territory capitals plus 13 major regional centres:

| Region | Locations |
|---|---|
| **Capitals (8)** | `sydney` · `melbourne` · `brisbane` · `perth` · `adelaide` · `hobart` · `darwin` · `canberra` |
| **NSW regional** | `newcastle` · `wollongong` |
| **QLD regional** | `gold_coast` · `sunshine_coast` · `cairns` · `townsville` · `mackay` |
| **VIC regional** | `geelong` · `ballarat` · `bendigo` |
| **TAS regional** | `launceston` |
| **Remote** | `alice_springs` (NT) · `broome` (WA) |

Coordinates are anchored to the canonical BOM observation point for each city (e.g. Sydney = Observatory Hill, Melbourne = Olympic Park) so cross-checking against BOM's official observations is straightforward. See [src/au_weather_mcp/data/curated/locations.yaml](src/au_weather_mcp/data/curated/locations.yaml) for the full registry.

## Worked examples

**"What's the weather in Sydney right now?"**

```
latest(location="sydney")
```

Returns:

```json
{
  "location_id": "sydney",
  "location_name": "Sydney",
  "state": "NSW",
  "latitude": -33.8607,
  "longitude": 151.205,
  "timezone": "Australia/Sydney",
  "period": {"start": "2026-05-12T11:30", "end": "2026-05-12T11:30"},
  "current": {
    "time": "2026-05-12T11:30",
    "temperature_c": 19.7,
    "apparent_temperature_c": 18.1,
    "relative_humidity_pct": 67,
    "precipitation_mm": 0.0,
    "cloud_cover_pct": 43,
    "pressure_msl_hpa": 1034.5,
    "wind_speed_kmh": 18.4,
    "wind_direction_deg": 149,
    "wind_gusts_kmh": 43.2,
    "weather_code": 1,
    "weather_description": "Mainly clear"
  },
  "source": "Open-Meteo (aggregates Bureau of Meteorology data under licence)",
  "attribution": "Weather data by Open-Meteo.com (https://open-meteo.com), licensed under CC BY 4.0...",
  "source_url": "https://api.open-meteo.com/v1/forecast?latitude=-33.8607&...",
  "server_version": "<package version, e.g. 0.3.3>",
  "location_resolution": "curated",
  "location_input": "sydney"
}
```

**"How was Sydney in January 2020?"**

```
get_weather(
  location="sydney",
  start_date="2020-01-01",
  end_date="2020-01-31",
  granularity="daily"
)
```

Returns 31 `DailyAggregate` rows with `temperature_max_c`, `temperature_min_c`, `precipitation_sum_mm`, and weather descriptions per day.

**"7-day Melbourne forecast, hourly detail"**

```
get_weather(
  location="melbourne",
  start_date="2026-05-12",
  end_date="2026-05-19",
  granularity="hourly"
)
```

Returns 168 hourly rows.

## Date formats

`start_date` and `end_date` must be `YYYY-MM-DD`. The server validates both the regex and the semantic date — `2024-13-40` raises a clean `ValueError` rather than silently failing.

| Date range | Routes to | Coverage |
|---|---|---|
| `end_date >= today - 5 days` | Open-Meteo forecast endpoint | Today + 16 days |
| `end_date < today - 5 days` | Open-Meteo historical archive | 1940-01-01 onwards |

## Trust contract

Every response carries:

- `source_url` — the exact Open-Meteo URL the data came from
- `attribution` — CC-BY 4.0 notice for both Open-Meteo and BOM
- `retrieved_at` — UTC timestamp when we fetched
- `server_version` — which wheel served the call (debugging stale `uvx` caches)
- `stale` — true if we're serving cached data because upstream failed; comes with `stale_reason`

Sanity validators reject upstream values outside the plausible Australian range (temperature outside −30°C to +55°C, humidity outside 0-100%, pressure outside 850-1080 hPa). Rather than silently passing bad data to your agent, we raise an explicit validation error so the agent can degrade gracefully.

## Development

```bash
git clone https://github.com/Bigred97/au-weather-mcp.git
cd au-weather-mcp
uv sync --extra dev
uv pip install -e .

# Unit tests (no network)
uv run pytest

# Live integration tests (hits Open-Meteo)
uv run pytest -m live
```

The SQLite cache lives at `~/.au-weather-mcp/cache.db`. Current observations refresh every 15 minutes (matching Open-Meteo's update cadence), forecasts every 1 hour, historical never (a year-old day in the archive doesn't change). Delete the file to force a refresh.

## Sister servers

The four packages run side-by-side in any MCP client; Claude disambiguates via the server prefix (`weather:latest` vs `abs:latest` vs `rba:latest` vs `ato:get_data`).

- **[abs-mcp](https://github.com/Bigred97/abs-mcp)** — Australian Bureau of Statistics: labour force, CPI, GDP, wages, housing, population
- **[rba-mcp](https://github.com/Bigred97/rba-mcp)** — Reserve Bank of Australia: cash rate, FX, lending rates
- **[ato-mcp](https://github.com/Bigred97/ato-mcp)** — Australian Taxation Office + ACNC: personal tax by postcode, company tax, charity register

## Data attribution

Weather data is provided by [Open-Meteo](https://open-meteo.com), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The underlying observations include data from the [Australian Bureau of Meteorology](https://www.bom.gov.au) under Open-Meteo's licensing arrangement.

**Postcode resolutions** (when `location_resolution == "postcode"`) additionally use [OpenStreetMap](https://www.openstreetmap.org/copyright) via the Nominatim service, licensed under the [Open Database Licence (ODbL)](https://opendatacommons.org/licenses/odbl/). The OSM attribution is automatically appended to the response's `attribution` field for any postcode lookup, so redistribution is licence-compliant out of the box.

If you redistribute responses, credit Open-Meteo + BOM (always) and OpenStreetMap (when postcode lookup was used).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — Harry Vass, 2026.
