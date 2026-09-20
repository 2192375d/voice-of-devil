import base64
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_input import GeminiVision, Observation
from observation import observe_world

PNG = b"\x89PNG\r\n\x1a\noriginal bytes"
DESCRIPTION = {"summary": "A green object is ahead.", "objects": [
    {"label": "green object", "screen_region": "center"}],
    "possible_hazards": [], "uncertainties": []}
HINTS = {"source": "godot", "bbox_format": "normalized_xyxy", "objects": [
    {"id": "7", "label": "Red sphere", "bbox": [0.2, 0.3, 0.4, 0.5]}]}


def frame():
    return {"ok": True, "result": {"status": "observed", "observation_sequence": 42,
            "simulation_time": 12.5, "held_item": None, "hints": copy.deepcopy(HINTS),
            "future_field": {"retained": True}},
            "image": {"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}}


def vision():
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=AsyncMock(return_value=SimpleNamespace(text=json.dumps(DESCRIPTION))))))
    return GeminiVision(client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("hinted", [True, False])
async def test_observe_preserves_image_and_separates_state(hinted, caplog):
    payload = frame()
    if not hinted:
        payload["result"].pop("hints")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    model = vision()
    with caplog.at_level("INFO"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await observe_world(client, "game:3000", model)
    assert calls == [{"command": "observe"}]
    assert result["observation_sequence"] == result["vision"]["source_observation_sequence"] == 42
    assert result["game_state"] == {"held_item": None, "future_field": {"retained": True}}
    assert result["vision"]["objects"] == DESCRIPTION["objects"]  # Gemini can correct labels.
    args = model.client.aio.models.generate_content.call_args.kwargs
    assert args["contents"][-1].inline_data.data == PNG
    assert args["contents"][-1].inline_data.mime_type == "image/png"
    if hinted:
        assert json.loads(args["contents"][0].text) == {"hints": HINTS}
    else:
        assert len(args["contents"]) == 1
    assert "goal" not in json.dumps(result)
    assert "gemini_ms=" in caplog.text
    assert "Red sphere" not in caplog.text
    assert payload["image"]["data"] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda p: p.update(ok=False),
    lambda p: p.update(image=None),
    lambda p: p["result"].update(status="busy"),
    lambda p: p["result"].pop("observation_sequence"),
    lambda p: p["image"].update(mime_type="image/jpeg"),
    lambda p: p["image"].update(data=""),
    lambda p: p["image"].update(data="not base64!"),
])
async def test_bad_game_response_never_calls_gemini(change):
    payload = frame()
    change(payload)
    model = vision()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError):
            await observe_world(client, "game:3000", model)
    model.client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_failure_never_calls_gemini():
    model = vision()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(500))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await observe_world(client, "game:3000", model)
    model.client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "not json", '{"summary":"incomplete"}'])
async def test_invalid_gemini_response_is_not_a_valid_observation(response):
    model = vision()
    model.client.aio.models.generate_content.return_value.text = response
    with pytest.raises((ValueError, ValidationError)):
        await model.summarize(frame()["image"]["data"])


@pytest.mark.asyncio
async def test_jev_receives_json_and_separate_voice():
    from jev_interface import JevInterface
    interface = JevInterface.__new__(JevInterface)
    interface.context = "test"
    answer = {"action": {"probabilities": {"stop": 1.0}}}
    interface.client = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(content=json.dumps(answer))))
    world = {"game_state": {"held_item": None}, "vision": DESCRIPTION}
    await interface.send_req("Pick up the sphere", world)
    state = interface.client.send_message.call_args.kwargs["system_one"]["state"]
    assert state["user_request"] == "Pick up the sphere"
    assert json.loads(state["game_status"]) == world
    assert "Pick up" not in state["game_status"]


@pytest.mark.asyncio
async def test_movement_does_not_request_vision(monkeypatch):
    import mcp_server
    factory = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kwargs: factory(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"ok": True, "result": {"status": "started"}, "image": None}))))
    model = SimpleNamespace(summarize=AsyncMock())
    monkeypatch.setattr(mcp_server, "vision_service", model)
    assert (await mcp_server.walk_forwards(5))["result"]["status"] == "started"
    model.summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_control_wrappers_send_distinct_godot_commands(monkeypatch):
    import mcp_server
    commands = []

    def handler(request):
        commands.append(json.loads(request.content))
        return httpx.Response(200, json={
            "ok": True, "result": {"status": "stopped"}, "image": None})

    factory = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kwargs: factory(
        **kwargs, transport=httpx.MockTransport(handler)))
    await mcp_server.clear_queue()
    await mcp_server.cancel_walk()
    await mcp_server.cancel_rotation()
    await mcp_server.stop_walking()
    assert commands == [
        {"command": "clear_queue", "arguments": {}},
        {"command": "cancel_walk", "arguments": {}},
        {"command": "cancel_rotation", "arguments": {}},
        {"command": "stop", "arguments": {}},
    ]


@pytest.mark.asyncio
async def test_mcp_returns_structured_observation(monkeypatch):
    import mcp_server
    world = {"observation_sequence": 42, "simulation_time": 12.5,
             "game_state": {"held_item": None}, "vision": DESCRIPTION}
    monkeypatch.setattr(mcp_server, "vision_service", SimpleNamespace(observe=AsyncMock(return_value=world)))
    result = await mcp_server.mcp.call_tool("observe", {})
    assert not result.is_error
    assert result.structured_content == world


@pytest.mark.asyncio
async def test_benchmark_uses_same_frames_and_reports_recognition(monkeypatch, tmp_path):
    import benchmark_observe
    sample = {"capture_ms": 20, "frame": frame(), "expected_labels": ["green object"],
              "expected_absent_labels": ["red sphere"]}
    (tmp_path / "frame_0000.json").write_text(json.dumps(sample))
    model = SimpleNamespace(
        summarize=AsyncMock(return_value=Observation.model_validate(DESCRIPTION)),
        client=SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock()))
    monkeypatch.setattr(benchmark_observe, "GeminiVision", lambda: model)
    output = tmp_path / "report.json"
    await benchmark_observe.run(SimpleNamespace(command="evaluate", directory=tmp_path, output=output))
    calls = model.summarize.call_args_list
    assert calls[0].args[0] == calls[1].args[0] == sample["frame"]["image"]["data"]
    assert calls[0].kwargs["hints"] is None
    assert calls[1].kwargs["hints"] == HINTS
    report = json.loads(output.read_text())
    assert report["summary"]["hinted"]["recognition_errors"] == 0
    assert report["summary"]["hinted"]["capture_plus_gemini_ms"]["p95"] >= 20
    model.client.aio.aclose.assert_awaited_once()
