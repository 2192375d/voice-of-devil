"""Command queue admission, FIFO, cancellation, and MCP tool surface."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_server_jev_client.app import Services, create_app, observe_and_summarize
from mcp_server_jev_client.command_queue import CommandService, is_explicit_stop
from mcp_server_jev_client.executor import StopOutcome
from mcp_server_jev_client.game_client import GameFrame, GodotObserver
from mcp_server_jev_client.models import ExecutionResult, ScoredDecision
from mcp_server_jev_client.providers import GeminiVision, JevDecider, SCORED_ACTIONS, YAW_CHOICES
from test_observe import FakeGameClient, OBSERVATION, PNG, STATE


class RecordingExecutor:
    def __init__(self):
        self.jobs = []
        self.stops = 0
        self.paused = False
        self.block = asyncio.Event()
        self.block.set()

    async def run_job(self, job):
        self.jobs.append(job.command_id)
        await self.block.wait()
        job.status = "completed"
        job.execution = ExecutionResult(game_status="started")

    async def priority_stop(self):
        self.stops += 1
        self.paused = False
        return StopOutcome(game_cleared=True, game_stopped=True)

    async def shutdown(self):
        return None


def action_probs(choice, p=0.91):
    rest = (1 - p) / (len(SCORED_ACTIONS) - 1)
    return {name: (p if name == choice else rest) for name in SCORED_ACTIONS}


def yaw_probs(choice="90", p=0.9):
    rest = (1 - p) / (len(YAW_CHOICES) - 1)
    return {key: (p if key == choice else rest) for key in YAW_CHOICES}


def scored(action="walk_forward", p=0.91, yaw="0"):
    payload = {
        "action": {"type": "choice", "choice": action, "probabilities": action_probs(action, p), "confidence": 0.8},
    }
    if action == "rotate":
        payload["yaw_degrees"] = {
            "type": "choice",
            "choice": yaw,
            "probabilities": yaw_probs(yaw),
            "confidence": 0.8,
        }
    return payload


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


async def make_service(**kwargs):
    transport = FakeGameClient()
    generate = AsyncMock(return_value=SimpleNamespace(text=__import__("json").dumps(OBSERVATION)))
    send = AsyncMock(return_value=SimpleNamespace(
        system_one=SimpleNamespace(answers=scored(), model="jev-1.13.0"),
    ))
    executor = kwargs.pop("executor", RecordingExecutor())
    clock = kwargs.pop("clock", FakeClock())
    service = CommandService(
        observer=GodotObserver(transport),
        vision=GeminiVision(SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
        jev=JevDecider(SimpleNamespace(send_message=send)),
        executor=executor,
        pipeline_lock=asyncio.Lock(),
        observe_and_summarize=observe_and_summarize,
        clock=clock,
        **kwargs,
    )
    service.start()
    return service, transport, send, executor, clock


@pytest.mark.parametrize("text,matched", [
    ("stop", True),
    ("STOP", True),
    ("stop moving", True),
    ("cancel all actions", True),
    ("do not stop", False),
    ("please stop moving now", False),
    ("stop.", False),
])
def test_explicit_stop_matches_entire_normalized_utterance(text, matched):
    assert is_explicit_stop(text) is matched


async def test_fifo_order_and_same_id_dedup():
    service, transport, send, executor, _ = await make_service()
    try:
        first, second = uuid4(), uuid4()
        a = await service.submit(first, "walk forward")
        b = await service.submit(second, "turn right")
        again = await service.submit(first, "walk forward")
        conflict = await service.submit(first, "something else")
        assert a.accepted and b.accepted and again.existing and not conflict.accepted
        assert conflict.error == "payload_conflict"
        for _ in range(50):
            if (await service.get(second)).status == "completed":
                break
            await asyncio.sleep(0.01)
        assert executor.jobs == [first, second]
        assert transport.commands.count("observe") >= 2
        assert "wait" not in transport.commands
    finally:
        await service.aclose()


async def test_queue_full_and_expiry_use_clock():
    clock = FakeClock()
    service, *_rest = await make_service(queue_capacity=2, queue_wait_seconds=30, clock=clock)
    worker = service._worker
    service._worker = None
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    try:
        ids = [uuid4() for _ in range(3)]
        assert (await service.submit(ids[0], "one")).accepted
        assert (await service.submit(ids[1], "two")).accepted
        full = await service.submit(ids[2], "three")
        assert not full.accepted and full.error == "queue_full"
        clock.advance(31)
        service.start()
        for _ in range(50):
            if (await service.get(ids[0])).status == "expired":
                break
            await asyncio.sleep(0.01)
        assert (await service.get(ids[0])).status == "expired"
        assert (await service.get(ids[1])).status == "expired"
    finally:
        await service.aclose()


async def test_cancel_discards_late_score(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_score(self, command, state, observation):
        entered.set()
        await release.wait()
        return JevDecider.score(self, command, state, observation)

    service, _, send, executor, _ = await make_service()
    original = service.jev.score

    async def gated(command, state, observation):
        entered.set()
        await release.wait()
        return await original(command, state, observation)

    service.jev.score = gated
    command_id = uuid4()
    try:
        await service.submit(command_id, "walk forward")
        await asyncio.wait_for(entered.wait(), 1)
        cancelled = await service.cancel(command_id)
        assert cancelled.status == "cancelled"
        release.set()
        await asyncio.sleep(0.05)
        assert executor.jobs == []
        assert (await service.get(command_id)).status == "cancelled"
    finally:
        release.set()
        await service.aclose()


async def test_wait_completes_locally_without_executor():
    service, transport, send, executor, _ = await make_service()
    send.return_value = SimpleNamespace(
        system_one=SimpleNamespace(answers=scored("wait", 0.93, yaw="0"), model="jev-test"),
    )
    command_id = uuid4()
    try:
        await service.submit(command_id, "wait")
        for _ in range(50):
            status = await service.get(command_id)
            if status.status == "completed":
                break
            await asyncio.sleep(0.01)
        assert (await service.get(command_id)).status == "completed"
        assert executor.jobs == []
        assert "wait" not in transport.commands
        assert all(command == "observe" for command in transport.commands)
    finally:
        await service.aclose()


async def test_unknown_id_after_restart_is_not_replayed():
    service, *_ = await make_service()
    command_id = uuid4()
    await service.submit(command_id, "walk forward")
    instance = service.instance_id
    await service.aclose()
    service2, *_rest = await make_service()
    try:
        missing = await service2.get(command_id)
        assert missing.found is False
        assert missing.service_instance_id != instance
        replay = await service2.submit(command_id, "walk forward")
        assert replay.accepted and not replay.existing
    finally:
        await service2.aclose()


async def test_mcp_initialize_list_and_call_and_stop_during_slow_jev():
    transport = FakeGameClient()
    generate = AsyncMock(return_value=SimpleNamespace(text=__import__("json").dumps(OBSERVATION)))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(system_one=SimpleNamespace(answers=scored(), model="jev-test"))

    send = AsyncMock(side_effect=slow_send)
    executor = RecordingExecutor()

    @asynccontextmanager
    async def resources():
        lock = asyncio.Lock()
        commands = CommandService(
            observer=GodotObserver(transport),
            vision=GeminiVision(SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
            jev=JevDecider(SimpleNamespace(send_message=send)),
            executor=executor,
            pipeline_lock=lock,
            observe_and_summarize=observe_and_summarize,
        )
        yield Services(
            GodotObserver(transport),
            GeminiVision(SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
            JevDecider(SimpleNamespace(send_message=send)),
            commands=commands,
        )

    app = create_app(services_factory=resources)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http_client:
            async with streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    init = await session.initialize()
                    assert init.server_info.name == "vod-voice-commands"
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == {
                        "submit_voice_command", "get_voice_command",
                        "cancel_voice_command", "stop_game", "get_command_context",
                    }
                    context = await session.call_tool("get_command_context", {})
                    command_id = uuid4()
                    submitted = await session.call_tool(
                        "submit_voice_command",
                        {"command_id": str(command_id), "transcript": "walk forward",
                         **context.structured_content},
                    )
                    assert submitted.structured_content["accepted"] is True
                    await asyncio.wait_for(entered.wait(), 2)
                    stopped = await session.call_tool("stop_game", {})
                    assert stopped.structured_content["game_stopped"] is True
                    assert executor.stops == 1
                    release.set()
                    for _ in range(50):
                        status = await session.call_tool(
                            "get_voice_command", {"command_id": str(command_id)},
                        )
                        if status.structured_content["status"] == "cancelled":
                            break
                        await asyncio.sleep(0.02)
                    assert status.structured_content["status"] == "cancelled"
                    assert executor.jobs == []
                    assert "tools" not in send.await_args.kwargs


async def test_jev_stop_cancels_python_queue_before_later_action():
    from mcp_server_jev_client.executor import GameExecutor
    from test_executor import ScriptedGame

    clock = FakeClock()
    game = ScriptedGame()
    executor = GameExecutor(game, game, clock=clock)
    service, _, send, _, _ = await make_service(executor=executor, clock=clock)
    send.side_effect = [
        SimpleNamespace(system_one=SimpleNamespace(answers=scored("stop"), model="jev-test")),
        SimpleNamespace(system_one=SimpleNamespace(answers=scored("walk_forward"), model="jev-test")),
    ]
    try:
        first, second = uuid4(), uuid4()
        await service.submit(first, "please stop")
        await service.submit(second, "walk forward")
        async with asyncio.timeout(2):
            while (await service.get(first)).status != "completed":
                await asyncio.sleep(0)
        assert (await service.get(second)).status == "cancelled"
        assert (await service.get(first)).decision.action == "stop"
        assert service.generation == 1
        assert send.await_count == 1
        assert game.commands == ["clear_queue", "stop"]
    finally:
        await service.aclose()


async def test_slow_vision_counts_toward_source_frame_age():
    from mcp_server_jev_client.executor import GameExecutor
    from mcp_server_jev_client.models import Observation
    from test_executor import ScriptedGame

    clock = FakeClock()
    game = ScriptedGame()
    executor = GameExecutor(game, game, clock=clock)
    service, _, _, _, _ = await make_service(executor=executor, clock=clock)

    async def slow_summary(png):
        clock.advance(20)
        return Observation.model_validate(OBSERVATION)

    service.vision.summarize = slow_summary
    try:
        command_id = uuid4()
        await service.submit(command_id, "walk forward")
        async with asyncio.timeout(2):
            while (await service.get(command_id)).status not in {"expired", "completed", "failed"}:
                await asyncio.sleep(0)
        status = await service.get(command_id)
        assert status.status == "expired"
        assert status.error == "stale_decision"
        assert game.commands == []
    finally:
        await service.aclose()
