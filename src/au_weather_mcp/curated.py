"""Curated location registry — the 45 AU places we hand-pick coordinates for.

The YAML at `data/curated/locations.yaml` is the source of truth. Loaded
once on first access and cached in `_REGISTRY`. Anyone can add a location
by editing the YAML and re-running; nothing in this file needs to change.

Search uses rapidfuzz to match queries against location id + name + state
so "syd", "Sydney", "NSW Sydney", and "sydney" all resolve to the same row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

_LOCATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class CuratedLocation:
    id: str
    name: str
    state: str
    description: str | None
    latitude: float
    longitude: float
    timezone: str
    elevation_m: float | None
    nearest_bom_station: str | None


_REGISTRY: dict[str, CuratedLocation] | None = None


def _yaml_path() -> Path:
    """Locate data/curated/locations.yaml both in dev and in installed wheels."""
    try:
        ref = resources.files("au_weather_mcp").joinpath("data/curated/locations.yaml")
        if ref.is_file():
            return Path(str(ref))
    except (ModuleNotFoundError, AttributeError):
        pass
    here = Path(__file__).resolve()
    candidates = [
        here.parent / "data" / "curated" / "locations.yaml",
        here.parent.parent.parent / "data" / "curated" / "locations.yaml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("Could not locate data/curated/locations.yaml")


def _load() -> dict[str, CuratedLocation]:
    raw = yaml.safe_load(_yaml_path().read_text(encoding="utf-8"))
    locs = raw.get("locations") or {}
    out: dict[str, CuratedLocation] = {}
    for loc_id, fields in locs.items():
        if not _LOCATION_ID_PATTERN.match(loc_id):
            raise ValueError(
                f"Location id {loc_id!r} must match the snake_case pattern "
                f"{_LOCATION_ID_PATTERN.pattern} (lowercase letter followed "
                "by letters/digits/underscores). Try renaming to e.g. "
                "'gold_coast', 'alice_springs', 'mount_gambier' in "
                "data/curated/locations.yaml."
            )
        out[loc_id] = CuratedLocation(
            id=loc_id,
            name=str(fields["name"]),
            state=str(fields["state"]),
            description=fields.get("description"),
            latitude=float(fields["latitude"]),
            longitude=float(fields["longitude"]),
            timezone=str(fields["timezone"]),
            elevation_m=(float(fields["elevation_m"]) if "elevation_m" in fields else None),
            nearest_bom_station=fields.get("nearest_bom_station"),
        )
    return out


def get(location_id: str) -> CuratedLocation | None:
    """Resolve a location id (case-insensitive) to its curated row."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load()
    return _REGISTRY.get(location_id.strip().lower())


def list_ids() -> list[str]:
    """Return all curated location ids, sorted alphabetically."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load()
    return sorted(_REGISTRY.keys())


def all_locations() -> list[CuratedLocation]:
    """Return every curated location, sorted by id."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load()
    return [_REGISTRY[i] for i in sorted(_REGISTRY.keys())]


def reset_registry() -> None:
    """For tests — drop the module-level cache so the YAML reloads."""
    global _REGISTRY
    _REGISTRY = None


def search(query: str, limit: int = 10) -> list[CuratedLocation]:
    """Fuzzy-search the curated location set.

    Matches against id + name + state so 'syd', 'Sydney', 'NSW' all work.
    Returns up to `limit` rows ranked by score.
    """
    locs = all_locations()
    # Each haystack entry is "id name state description" — wide enough for
    # broad queries like 'tropical north qld' to hit Cairns/Townsville.
    haystack = {
        i: f"{loc.id} {loc.name} {loc.state} {loc.description or ''}".lower()
        for i, loc in enumerate(locs)
    }
    matches = process.extract(
        query.lower(), haystack, scorer=fuzz.WRatio, limit=max(limit * 2, 20)
    )
    seen: set[int] = set()
    out: list[CuratedLocation] = []
    for _hay, _score, idx in matches:
        if idx in seen:
            continue
        seen.add(idx)
        out.append(locs[idx])
        if len(out) >= limit:
            break
    return out
