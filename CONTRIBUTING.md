# Contributing to au-weather-mcp

Thanks for considering a contribution. This package is intentionally small and focused — please read this before opening a PR.

## Quick start

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

## Scope

In scope:

- Bug fixes
- Performance improvements
- Coverage of additional Australian locations (single YAML edit + a test)
- Test coverage for edge cases
- Documentation improvements

Out of scope (for this repo — please open an issue first to discuss):

- New MCP tools beyond the 5 in `server.py`
- Wrapping non-Open-Meteo weather sources (consider a separate package)
- Non-Australian locations (consider a separate weather-mcp package)
- Adding paid-tier features (the public package stays free; paid features live in the planned hosted CLI)

## Adding a curated location

The curated set is just a YAML. To add a location:

1. Edit `src/au_weather_mcp/data/curated/locations.yaml` — add a new entry following the existing pattern.
2. Pick coordinates from the nearest BOM observation point if possible (so cross-checks against BOM's own data are easy).
3. Pick the IANA timezone for the location's state.
4. Add a test in `tests/test_curated.py` that exercises your new location.
5. Run `uv run pytest`.
6. Bump the patch version in `pyproject.toml` and add an entry to `CHANGELOG.md`.

## Code style

- `uv run pytest` must pass before submitting.
- Every public function has a docstring.
- Every parameter on every `@mcp.tool` uses `Annotated[Type, Field(description=…, examples=[…])]` — this is what gives the Glama Tool Definition Quality score its A grade. Don't regress it.
- Comments explain *why*, not *what*. The code already says what it does.

## Reporting bugs

Open an issue at https://github.com/Bigred97/au-weather-mcp/issues with:

- The MCP tool call that failed (e.g. `latest("sydney")`)
- The error message or wrong output
- Your `au-weather-mcp` version (run `python -c "import au_weather_mcp; print(au_weather_mcp.__version__)"`)
- Whether it reproduces on a fresh `uvx --refresh au-weather-mcp` install
