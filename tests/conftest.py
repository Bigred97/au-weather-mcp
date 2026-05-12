"""Shared fixtures.

Resets the server's module-level OpenMeteoClient between tests so a closed
event loop from one test doesn't poison the next one. Mirrors the same
pattern used in abs-mcp and rba-mcp.
"""
from __future__ import annotations

import pytest

from au_weather_mcp import server


@pytest.fixture(autouse=True)
async def _reset_server_client():
    yield
    await server.reset_client_for_tests()
