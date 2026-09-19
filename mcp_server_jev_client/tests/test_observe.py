import asyncio
import base64
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from mcp_server_jev_client.app import Services, create_app
from mcp_server_jev_client.models import Observation
from mcp_server_jev_client.providers import (
    GeminiVision,
    GodotObserver,
    JevDecider,
    normalize_decision,
)


# A 1x1 PNG intentionally proves the service does not enforce 512x512 dimensions.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6"
    "h1sAAAAASUVORK5CYII="
)
STATE = {
    "status": "observed",
    "observation_sequence": 42,
    "simulation_time": 12.5,
    "position": {"x": 0, "y": 1, "z": 12},
    "rotation_degrees": {"x": 0, "y": -45, "z": 0},
    "velocity": {"x": 0, "y": 0, "z": 0},
    "grounded": True,
    "held_item": None,
    "camera": {"position": {"x": 0, "y": 2, "z": 12}, "width": 512, "height": 512},
    "active_instructions": [],
    "pending_action_count": 0,
    "last_finished_instruction": None,
    "future_field": {"preserved": True},
}
OBSERVATION = {
    "summary": "A red crystal is partly hidden behind a rock on the right.",
    "objects": [{"label": "red crystal", "screen_region": "right"}],
    "possible_hazards": ["Uneven ground ahead"],
    "uncertainties": ["Distance to the crystal is unknown"],
}


def answers(action="rotate", yaw="90"):
    return {
        "action": {"type": "choice", "choice": action},
        "yaw_degrees": {"type": "choice", "choice": yaw},
    }


class Harness:
    def __init__(self, timeout=60):
        self.result = CallToolResult(
            structuredContent=copy.deepcopy(STATE),
            content=[
                TextContent(type="text", text=json.dumps(STATE)),
                ImageContent(type="image", mimeType="image/png", data=base64.b64encode(PNG).decode()),
            ],
        )
        self.session = SimpleNamespace(call_tool=AsyncMock(return_value=self.result))
        self.generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(OBSERVATION)))
        self.send = AsyncMock(return_value=SimpleNamespace(system_one=SimpleNamespace(answers=answers())))
        self.services = Services(
            GodotObserver(self.session),
            GeminiVision(SimpleNamespace(models=SimpleNamespace(generate_content=self.generate))),
            JevDecider(SimpleNamespace(send_message=self.send)),
        )
        self.closed = False

        @asynccontextmanager
        async def resources():
            try:
                yield self.services
            finally:
                self.closed = True

        self.app = create_app(services_factory=resources, timeout_seconds=timeout)

    @asynccontextmanager
    async def client(self):
        async with self.app.router.lifespan_context(self.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                yield client

    def assert_observe_only(self):
        assert all(call.args == ("observe", {}) for call in self.session.call_tool.await_args_list)


async def test_full_pipeline_preserves_image_state_and_first_person_context():
    h = Harness()
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": " Find the red crystal "})
    assert response.status_code == 200
    body = response.json()
    assert body["observation_sequence"] == 42
    assert body["game_state"] == STATE
    assert body["observation"] == OBSERVATION
    assert body["decision"] == {"action": "rotate", "arguments": {"degrees": {"x": 0, "y": 90, "z": 0}}}
    assert set(body) == {"observation_sequence", "game_state", "observation", "decision", "timings_ms"}
    assert set(body["timings_ms"]) == {"mcp", "gemini", "jev", "total"}
    assert all(value >= 0 for value in body["timings_ms"].values())
    gemini = h.generate.await_args.kwargs
    assert gemini["model"] == "gemini-3.5-flash"
    assert gemini["contents"][0].inline_data.data == PNG
    assert gemini["contents"][0].inline_data.mime_type == "image/png"
    assert "first-person" in gemini["config"].system_instruction
    assert gemini["config"].response_mime_type == "application/json"
    assert gemini["config"].response_json_schema == Observation.model_json_schema()
    assert json.loads(h.send.await_args.args[0]) == {
        "goal": "Find the red crystal", "game_state": STATE, "observation": OBSERVATION,
    }
    jev = h.send.await_args.kwargs
    assert jev["llm_provider"] == "typesafe" and jev["model_name"] == "jev-latest"
    assert jev["stream"] is False and jev["memory"] == "off"
    assert "thread_id" not in jev and "tools" not in jev
    h.session.call_tool.assert_awaited_once_with("observe", {})
    h.assert_observe_only()
    assert h.closed


@pytest.mark.parametrize("payload", [{}, {"goal": ""}, {"goal": "   "}, {"goal": 10}])
async def test_invalid_request_does_not_reach_providers(payload):
    h = Harness()
    async with h.client() as client:
        response = await client.post("/observe", json=payload)
    assert response.status_code == 422
    h.session.call_tool.assert_not_awaited()
    h.generate.assert_not_awaited()
    h.send.assert_not_awaited()


@pytest.mark.parametrize("action", ["walk_forward", "stop", "grab_item", "drop_item", "wait"])
async def test_simple_decisions_are_returned_not_executed(action):
    h = Harness()
    h.send.return_value.system_one.answers = answers(action, yaw="unused")
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 200
    assert response.json()["decision"] == {"action": action, "arguments": {}}
    h.session.call_tool.assert_awaited_once_with("observe", {})


@pytest.mark.parametrize("yaw", ["-180", "-90", "0", "90", "180"])
def test_relative_rotation_sign_and_bounds(yaw):
    result = normalize_decision(answers(yaw=yaw))
    assert result.arguments.degrees.y == int(yaw)
    assert result.arguments.degrees.x == result.arguments.degrees.z == 0


@pytest.mark.parametrize("bad_answers", [
    None, {}, {"action": None}, answers("fly"), answers([]),
    answers(yaw="91"), answers(yaw="182"), answers(yaw="-182"),
    answers(yaw="90.0"), answers(yaw=90), answers(yaw=[]),
    {"action": {"type": "noul", "choice": "wait"}},
    {"action": {"type": "choice", "choice": "rotate"}},
])
async def test_malformed_jev_results_are_stage_errors(bad_answers):
    h = Harness()
    h.send.return_value.system_one.answers = bad_answers
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 502
    assert response.json()["detail"] == {"stage": "jev", "error": "upstream_error"}
    h.assert_observe_only()


async def test_missing_system_one_is_not_parsed_from_content():
    h = Harness()
    h.send.return_value = SimpleNamespace(system_one=None, content=json.dumps(answers()))
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 502
    assert response.json()["detail"]["stage"] == "jev"
    h.assert_observe_only()


@pytest.mark.parametrize("failure", ["tool_error", "missing_state", "missing_position", "missing_image", "empty_image", "invalid_base64"])
async def test_bad_mcp_result_stops_before_model_calls(failure):
    h = Harness()
    if failure == "tool_error":
        h.result.is_error = True
    elif failure == "missing_state":
        h.result.structured_content = None
    elif failure == "missing_position":
        del h.result.structured_content["position"]
    elif failure == "missing_image":
        h.result.content = h.result.content[:1]
    elif failure == "empty_image":
        h.result.content[1].data = ""
    else:
        h.result.content[1].data = "not base64!"
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 502
    assert response.json()["detail"]["stage"] == "mcp"
    h.generate.assert_not_awaited()
    h.send.assert_not_awaited()
    h.assert_observe_only()


@pytest.mark.parametrize("text", [None, "", "not JSON", "{}", json.dumps({**OBSERVATION, "objects": "rock"})])
async def test_invalid_gemini_output_never_reaches_jev(text):
    h = Harness()
    h.generate.return_value.text = text
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
    assert response.status_code == 502
    assert response.json()["detail"]["stage"] == "gemini"
    h.send.assert_not_awaited()
    h.assert_observe_only()


@pytest.mark.parametrize("stage", ["mcp", "gemini", "jev"])
async def test_upstream_errors_are_sanitized_and_release_pipeline(stage, caplog):
    h = Harness()
    mock = {"mcp": h.session.call_tool, "gemini": h.generate, "jev": h.send}[stage]
    mock.side_effect = RuntimeError("sensitive-api-key-and-image-payload")
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
        assert response.status_code == 502
        assert response.json()["detail"]["stage"] == stage
        assert "sensitive-api-key" not in response.text + caplog.text
        mock.side_effect = None
        assert (await client.post("/observe", json={"goal": "Retry"})).status_code == 200
    h.assert_observe_only()


@pytest.mark.parametrize("stage", ["mcp", "gemini", "jev"])
async def test_deadline_cancels_current_stage_and_releases_pipeline(stage):
    h = Harness(timeout=0.05)
    mock = {"mcp": h.session.call_tool, "gemini": h.generate, "jev": h.send}[stage]
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock.side_effect = hang
    async with h.client() as client:
        response = await client.post("/observe", json={"goal": "Explore"})
        assert response.status_code == 504
        assert response.json()["detail"] == {"stage": stage, "error": "upstream_timeout"}
        assert cancelled.is_set()
        mock.side_effect = None
        assert (await client.post("/observe", json={"goal": "Retry"})).status_code == 200
    h.assert_observe_only()


async def test_overlapping_request_is_rejected_without_queuing():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_release(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(text=json.dumps(OBSERVATION))

    h.generate.side_effect = wait_for_release
    async with h.client() as client:
        first = asyncio.create_task(client.post("/observe", json={"goal": "Explore"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = await client.post("/observe", json={"goal": "Explore"})
            assert second.status_code == 409
            h.session.call_tool.assert_awaited_once_with("observe", {})
        finally:
            release.set()
            assert (await first).status_code == 200
    h.assert_observe_only()


async def test_cancelled_request_releases_pipeline():
    h = Harness()
    entered = asyncio.Event()

    async def hang(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    h.generate.side_effect = hang
    async with h.client() as client:
        request = asyncio.create_task(client.post("/observe", json={"goal": "Explore"}))
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        h.generate.side_effect = None
        assert (await client.post("/observe", json={"goal": "Retry"})).status_code == 200
    h.assert_observe_only()
