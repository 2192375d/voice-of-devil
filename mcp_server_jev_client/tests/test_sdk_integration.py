"""Exercise real SDK serialization and lifecycle against in-memory HTTP peers."""

import base64
import json
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from backboard import BackboardClient
from google import genai
from google.genai import types
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import REQUEST_TIMEOUT

from mcp_server_jev_client.app import Services, create_app
from mcp_server_jev_client.providers import GeminiVision, GodotObserver, JevDecider
from test_observe import Harness, OBSERVATION, PNG, STATE, answers


async def test_real_sdks_serialize_observation_and_decision_without_network():
    calls = []

    def godot_handler(request):
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Godot-test", "version": "1"}}
        elif body["method"] == "notifications/initialized":
            return httpx2.Response(202)
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "observe", "inputSchema": {"type": "object"}}]}
        else:
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "observe"
            assert body["params"]["arguments"] == {}
            assert request.headers["mcp-protocol-version"] == "2025-11-25"
            result = {"isError": False, "structuredContent": STATE, "content": [
                {"type": "text", "text": json.dumps(STATE)},
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()},
            ]}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def gemini_handler(request):
        assert request.url.path.endswith("/models/gemini-3.5-flash:generateContent")
        body = json.loads(request.content)
        image = body["contents"][0]["parts"][0]["inlineData"]
        assert base64.urlsafe_b64decode(image["data"]) == PNG
        assert image["mimeType"] == "image/png"
        assert "first-person" in body["systemInstruction"]["parts"][0]["text"]
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={"candidates": [{"content": {
            "role": "model", "parts": [{"text": json.dumps(OBSERVATION)}],
        }, "finishReason": "STOP"}]})

    def backboard_handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/threads/messages")
        assert json.loads(body["content"])["game_state"] == STATE
        assert json.loads(body["content"])["observation"] == OBSERVATION
        assert body["system_one"]["questions"]["action"]["type"] == "choice"
        assert body["memory"] == "off" and body["stream"] is False
        assert "tools" not in body and "thread_id" not in body
        return httpx.Response(200, json={"messages": [{"system_one": {
            "model": "jev-latest", "answers": answers(yaw="-90"), "usage": {},
        }}]})

    gemini = genai.Client(api_key="test", http_options=types.HttpOptions(
        async_client_args={"transport": httpx.MockTransport(gemini_handler)},
    ))
    backboard = BackboardClient(api_key="test")
    await backboard._client.aclose()
    backboard._client = httpx.AsyncClient(transport=httpx.MockTransport(backboard_handler))

    @asynccontextmanager
    async def services():
        async with gemini.aio as google_client, backboard:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(godot_handler)) as transport:
                async with streamable_http_client("http://godot/mcp", http_client=transport) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        result = await session.initialize()
                        assert result.protocol_version == "2025-11-25"
                        yield Services(GodotObserver(session), GeminiVision(google_client), JevDecider(backboard))

    app = create_app(services_factory=services)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/observe", json={"goal": "Explore"})
                assert response.status_code == 200, response.text
                assert response.json()["decision"]["arguments"]["degrees"]["y"] == -90
        tool_calls = [call for call in calls if call["method"] == "tools/call"]
        assert len(tool_calls) == 1 and tool_calls[0]["params"]["name"] == "observe"
        assert backboard._client.is_closed
    finally:
        gemini.close()


async def test_backboard_wrapped_network_timeout_is_504():
    def timeout(request):
        raise httpx.ReadTimeout("secret detail", request=request)

    async with BackboardClient(api_key="test") as backboard:
        await backboard._client.aclose()
        backboard._client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        h = Harness()
        h.services.jev = JevDecider(backboard)
        async with h.client() as client:
            response = await client.post("/observe", json={"goal": "Explore"})
        assert response.status_code == 504
        assert response.json()["detail"] == {"stage": "jev", "error": "upstream_timeout"}
        assert "secret" not in response.text
        h.assert_observe_only()


@pytest.mark.parametrize("error", [
    httpx2.ReadTimeout("timeout"), MCPError(REQUEST_TIMEOUT, "timeout"),
])
async def test_mcp_timeout_types_are_504(error):
    h = Harness()
    h.session.call_tool.side_effect = error
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 504
    assert response.json()["detail"]["stage"] == "mcp"
    h.generate.assert_not_awaited()
    h.assert_observe_only()
