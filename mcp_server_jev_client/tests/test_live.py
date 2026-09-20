"""Opt-in check: uses a running rendered game and makes paid model requests."""

import os

import httpx
import pytest

from mcp_server_jev_client.app import create_app
from mcp_server_jev_client.models import ObserveResponse


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE_OBSERVE") != "1", reason="Set RUN_LIVE_OBSERVE=1 to call the live providers")
async def test_rendered_game_and_live_providers():
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = app.state.services.transport
        original = transport._post
        names = []

        async def record(command, arguments=None):
            names.append(command)
            return await original(command, arguments)

        transport._post = record
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/observe", json={"goal": "Explore the visible surroundings"})
        assert names == ["observe"]
        assert response.status_code == 200, response.text
        result = ObserveResponse.model_validate(response.json())
        assert result.observation_sequence > 0
        assert result.observation.summary.strip()
