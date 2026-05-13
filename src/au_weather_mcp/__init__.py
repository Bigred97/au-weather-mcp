from importlib.metadata import PackageNotFoundError, version as _version

# `_version()` raises PackageNotFoundError when the dist-info is missing
# entirely, but importlib_metadata (and some Python builds) return None
# when the dist-info exists but is corrupted (e.g. left over from an
# older editable install with a missing RECORD file). The `or` fallback
# turns the None case into the same safe default so `__version__` is
# always a non-empty string — `server_version` on every response stays
# valid against the Pydantic `str` field.
try:
    __version__ = _version("au-weather-mcp") or "0.0.0+unknown"
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
