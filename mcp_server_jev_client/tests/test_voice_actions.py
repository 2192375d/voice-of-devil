"""Offline coverage for Godot-only observations and explicit Jev dispatch."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import jev_interface
import mcp_server


@pytest.fixture
def voice_server(monkeypatch):
    # Do not initialize microphone/transcription dependencies for dispatch tests.
    monkeypatch.setitem(sys.modules, "voice", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("test_voice_dispatch_server", Path(__file__).parents[1] / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("action,arguments,tool,expected", [
    ("walk_forward", {"meters": 5}, "walk_forwards", (5,)),
    ("walk_forward", {"meters": -2}, "walk_forwards", (-2,)),
    ("stop", {}, "stop_walking", ()),
    ("rotate", {"degrees": {"x": 0, "y": -90, "z": 0}}, "rotate", ()),
    ("grab_item", {}, "grab_item", ()),
    ("drop_item", {}, "drop_item", ()),
    ("interact", {}, "interact", ()),
])
async def test_only_selected_action_is_executed(voice_server, monkeypatch, action, arguments, tool, expected):
    tools = {}
    for name in ("walk_forwards", "stop_walking", "rotate", "grab_item", "drop_item", "interact", "observe"):
        tools[name] = AsyncMock(return_value={"ok": True, "result": {"status": "started"}})
        monkeypatch.setattr(mcp_server, name, tools[name])
    interface = SimpleNamespace(send_req=AsyncMock(return_value={
        "action": action, "confidence": 0.9, "arguments": arguments}))
    state = {"game_state": {"held_item": None}, "vision": None}
    await voice_server.agent_send_execute_loop(interface, "operator command", state)
    interface.send_req.assert_awaited_once_with("operator command", state)
    for name, call in tools.items():
        if name == tool:
            if name == "rotate":
                call.assert_awaited_once_with(x=0, y=-90, z=0)
            else:
                call.assert_awaited_once_with(*expected)
        else:
            call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,confidence", [("walk_forward", 0.5), ("walk_forward", 0.4), ("walk_and_turn", 0.5), ("wait", 1.0)])
async def test_wait_or_low_confidence_never_moves(voice_server, monkeypatch, capsys, action, confidence):
    send = AsyncMock()
    monkeypatch.setattr(mcp_server, "send_game_command", send)
    interface = SimpleNamespace(send_req=AsyncMock(return_value={
        "action": action, "confidence": confidence, "arguments": {"meters": 5}}))
    await voice_server.agent_send_execute_loop(interface, "walk forward", {})
    send.assert_not_awaited()
    assert "No action sent" in capsys.readouterr().out


def jev(answer):
    interface = jev_interface.JevInterface.__new__(jev_interface.JevInterface)
    interface.client = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(content=json.dumps(answer))))
    return interface


def test_yaw_prompt_has_no_fixed_quarter_turn_default():
    question = jev_interface.questions["yaw_degrees"]
    assert "no fixed default angle" in question["instructions"]
    assert "default 90" not in question["instructions"]
    assert set(question["criteria"]) == {str(angle) for angle in range(-180, 181, 2)}


@pytest.mark.asyncio
@pytest.mark.parametrize("angle", [-136, -24, 16, 62, 118])
async def test_jev_selected_angle_is_preserved_without_default(angle):
    interface = jev({"action": {"probabilities": {"rotate": 1.0}},
                     "yaw_degrees": {"probabilities": {str(angle): 0.8, "90": 0.2}}})
    result = await interface.send_req("turn toward the target", {"vision": None})
    assert result["arguments"] == {"degrees": {"x": 0, "y": angle, "z": 0}}


@pytest.mark.asyncio
async def test_jev_combined_movement_includes_both_arguments():
    interface = jev({"action": {"probabilities": {"walk_and_turn": 0.95, "wait": 0.05}},
                     "meters": {"probabilities": {"5": 1.0}},
                     "yaw_degrees": {"probabilities": {"90": 1.0}}})
    result = await interface.send_req("walk forward while turning right", {"vision": None})
    assert result == {"action": "walk_and_turn", "confidence": 0.95,
                      "arguments": {"meters": 5, "degrees": {"x": 0, "y": 90, "z": 0}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [None, "walk_forward", "rotate"])
async def test_combined_movement_overlaps_and_reports_partial_failure(voice_server, monkeypatch, capsys, failed):
    walking, turning = asyncio.Event(), asyncio.Event()
    calls = []
    async def walk(meters):
        calls.append(("walk_forward", meters))
        walking.set()
        await turning.wait()  # A sequential dispatcher would deadlock here.
        if failed == "walk_forward":
            raise ValueError("walk rejected")
        return {"ok": True, "result": {"status": "started"}}
    async def turn(**degrees):
        calls.append(("rotate", degrees))
        turning.set()
        await walking.wait()
        if failed == "rotate":
            raise ValueError("turn rejected")
        return {"ok": True, "result": {"status": "started"}}
    monkeypatch.setattr(mcp_server, "walk_forwards", walk)
    monkeypatch.setattr(mcp_server, "rotate", turn)
    stop = AsyncMock()
    monkeypatch.setattr(mcp_server, "stop_walking", stop)
    interface = SimpleNamespace(send_req=AsyncMock(return_value={
        "action": "walk_and_turn", "confidence": 0.9,
        "arguments": {"meters": 5, "degrees": {"x": 0, "y": 90, "z": 0}}}))
    operation = voice_server.agent_send_execute_loop(interface, "walk and turn right", {})
    if failed:
        with pytest.raises(RuntimeError, match="other action may already be running"):
            await asyncio.wait_for(operation, timeout=1)
        output = capsys.readouterr().out
        assert f"Godot {failed} failed:" in output
        assert "result: {'status': 'started'}" in output
    else:
        result = await asyncio.wait_for(operation, timeout=1)
        assert set(result["result"]) == {"walk_forward", "rotate"}
    assert calls == [("walk_forward", 5), ("rotate", {"x": 0, "y": 90, "z": 0})]
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_turn_preserves_existing_walk(voice_server, monkeypatch):
    turn = AsyncMock(return_value={"ok": True, "result": {"status": "started"}})
    walk, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(mcp_server, "rotate", turn)
    monkeypatch.setattr(mcp_server, "walk_forwards", walk)
    monkeypatch.setattr(mcp_server, "stop_walking", stop)
    interface = SimpleNamespace(send_req=AsyncMock(return_value={
        "action": "rotate", "confidence": 0.9,
        "arguments": {"degrees": {"x": 0, "y": -90, "z": 0}}}))
    state = {"game_state": {"active_instructions": [{"type": "walk_forward", "status": "running"}]}}
    await voice_server.agent_send_execute_loop(interface, "turn left", state)
    interface.send_req.assert_awaited_once_with("turn left", state)
    turn.assert_awaited_once_with(x=0, y=-90, z=0)
    walk.assert_not_awaited()
    stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,parameter,label,expected", [
    ("walk_forward", "meters", "5", {"meters": 5}),
    ("walk_forward", "meters", "-5", {"meters": -5}),
    ("rotate", "yaw_degrees", "-90", {"degrees": {"x": 0, "y": -90, "z": 0}}),
    ("stop", "meters", "5", {}),
])
async def test_jev_decision_has_one_action_and_valid_arguments(action, parameter, label, expected):
    interface = jev({"action": {"probabilities": {action: 0.95, "wait": 0.05}},
                     parameter: {"probabilities": {label: 1.0}}})
    result = await interface.send_req("command", {"vision": None})
    assert result == {"action": action, "confidence": 0.95, "arguments": expected}
    await interface.send_req("next command", {"vision": None})
    assert interface.context.count("USER REQUEST:") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("probabilities", [
    {}, {"unknown": 1}, {"walk_forward": float("nan")}, {"stop": -1},
    {"stop": 2}, {"stop": True}, {"stop": "0.9"},
])
async def test_malformed_jev_choices_fail_closed(probabilities):
    with pytest.raises(ValueError):
        await jev({"action": {"probabilities": probabilities}}).send_req("stop", {})


@pytest.mark.asyncio
async def test_godot_state_never_calls_gemini_and_preserves_hints(monkeypatch):
    hints = {"source": "godot", "objects": [{"id": "1", "label": "door"}]}
    payload = {"ok": True, "result": {"status": "observed", "observation_sequence": 7,
               "simulation_time": 3.0, "position": {"x": 1}, "hints": hints},
               "image": {"mime_type": "image/png", "data": "unused image"}}
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=payload)
    factory = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kwargs: factory(
        **kwargs, transport=httpx.MockTransport(handler)))
    start = AsyncMock(side_effect=AssertionError("Gemini must not start"))
    monkeypatch.setattr(mcp_server, "start_vision_service", start)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    state = await mcp_server.get_game_state()
    assert state == {"observation_sequence": 7, "simulation_time": 3.0,
                     "game_state": {"position": {"x": 1}, "hints": hints}, "vision": None}
    assert "unused image" not in json.dumps(state)
    assert calls == [{"command": "observe"}]
    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,error", [
    (200, {"ok": False, "result": {"status": "busy"}}, ValueError),
    (400, {"ok": False, "error": "invalid rotation"}, httpx.HTTPStatusError),
])
async def test_godot_rejections_are_surfaced_without_retry(monkeypatch, status, payload, error):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json=payload)
    factory = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kwargs: factory(
        **kwargs, transport=httpx.MockTransport(handler)))
    with pytest.raises(error):
        await mcp_server.rotate(y=90)
    assert requests == [{"command": "rotate", "arguments": {"degrees": {"x": 0, "y": 90, "z": 0}}}]


@pytest.mark.asyncio
async def test_voice_entrypoint_uses_only_godot_state(voice_server, monkeypatch):
    recorder = SimpleNamespace(start=Mock(), stop=Mock(return_value=object()), close=Mock())
    voice_server.voice = SimpleNamespace(Recorder=lambda: recorder, TARGET_RATE=16000,
        load_transcriber=lambda: Mock(), process=Mock(return_value="walk forward"))
    monkeypatch.setattr(voice_server, "flush_stdin", lambda: None)
    interface = object()
    monkeypatch.setattr(voice_server.jev_interface, "JevInterface", lambda: interface)
    state = {"game_state": {"active_instructions": []}, "vision": None}
    get_state = AsyncMock(return_value=state)
    monkeypatch.setattr(mcp_server, "get_game_state", get_state)
    vision = AsyncMock(side_effect=AssertionError("No Gemini in voice loop"))
    monkeypatch.setattr(mcp_server, "observe", vision)
    monkeypatch.setattr(mcp_server, "start_vision_service", vision)
    decide = AsyncMock()
    monkeypatch.setattr(voice_server, "agent_send_execute_loop", decide)
    inputs = iter(["", ""])
    def answer(_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(EOFError):
        await voice_server.main()
    get_state.assert_awaited_once()
    decide.assert_awaited_once_with(interface, "walk forward", state)
    vision.assert_not_awaited()
    recorder.close.assert_called_once()
