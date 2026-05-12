# Changelog

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
