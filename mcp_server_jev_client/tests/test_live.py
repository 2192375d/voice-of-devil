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
        # Record the real client calls to ensure this check only observes.
        session = app.state.services.game.session
        original_call = session.call_tool
        names = []

        async def record(name, arguments=None, **kwargs):
            names.append(name)
            return await original_call(name, arguments, **kwargs)

        session.call_tool = record
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/observe", json={"goal": "Explore the visible surroundings"})
        assert names == ["observe"]
        assert response.status_code == 200, response.text
        result = ObserveResponse.model_validate(response.json())
        assert result.observation_sequence > 0
        assert result.observation.summary.strip()
