"""Bounded executor, preconditions, walk stop, rotate completion, priority stop."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from mcp_server_jev_client.command_queue import CommandJob
from mcp_server_jev_client.executor import GameExecutor, ExecutionPolicy, WALK_INTERPRETATION
from mcp_server_jev_client.game_client import GameAck, GameClientError, GameFrame
from mcp_server_jev_client.models import Degrees, EmptyArguments, ExecutionResult, RotationArguments, ScoredDecision
from mcp_server_jev_client.providers import SCORED_ACTIONS, YAW_CHOICES
from test_observe import PNG, STATE


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ScriptedGame:
    def __init__(self, states=None, acks=None, errors=None):
        self.commands = []
        self.states = list(states or [STATE])
        self.acks = list(acks or [])
        self.errors = dict(errors or {})
        self.observe_count = 0

    async def observe(self):
        self.commands.append("observe")
        if "observe" in self.errors and self.observe_count >= 1:
            raise self.errors["observe"]
        state = self.states[min(self.observe_count, len(self.states) - 1)]
        self.observe_count += 1
        return GameFrame(png=PNG, state=deepcopy(state))

    async def execute(self, command, arguments=None):
        self.commands.append(command)
        if command in self.errors:
            raise self.errors[command]
        if self.acks:
            ack = self.acks.pop(0)
            return ack
        status = {
            "walk_forward": "started",
            "rotate": "started",
            "stop": "stopped",
            "clear_queue": "cleared",
            "grab_item": "picked_up",
            "drop_item": "dropped",
        }[command]
        return GameAck(ok=True, status=status, message=None, request_id="r", http_status=200, cleared_count=0)


def make_scored(action, yaw=90):
    rest = 0.01
    probs = {name: rest for name in SCORED_ACTIONS}
    probs[action] = 0.91
    kwargs = dict(
        action=action,
        action_probabilities=probs,
        selected_action_probability=0.91,
        distribution_confidence=0.8,
    )
    if action == "rotate":
        yaw_map = {key: 0.001 for key in YAW_CHOICES}
        yaw_map[str(yaw)] = 0.9
        kwargs["arguments"] = RotationArguments(degrees=Degrees(y=yaw))
        kwargs["yaw_probabilities"] = yaw_map
        kwargs["selected_yaw_probability"] = 0.9
    else:
        kwargs["arguments"] = EmptyArguments()
    return ScoredDecision(**kwargs)


def job_for(action, state=None, frame_started_at=None, **kwargs):
    job = CommandJob(command_id=uuid4(), transcript="go", queued_at=0.0, status="ready_to_execute")
    job.scored = make_scored(action, **kwargs)
    job.scoring_state = deepcopy(state or STATE)
    job.frame_started_at = frame_started_at
    return job


def idle_state(**overrides):
    state = deepcopy(STATE)
    state.update(overrides)
    return state


def moving_state(kind="rotate"):
    state = idle_state()
    state["active_instructions"] = [{"type": kind, "status": "running"}]
    return state


async def test_started_rotate_is_not_complete_until_idle():
    clock = FakeClock()
    game = ScriptedGame(states=[idle_state(), moving_state(), moving_state(), idle_state()])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    executor = GameExecutor(game, game, clock=clock, sleep=sleep, policy=ExecutionPolicy(rotate_poll_seconds=0.5))
    job = job_for("rotate")
    await executor.run_job(job)
    assert job.status == "completed"
    assert "rotate" in game.commands
    assert game.commands[0] == "observe"
    assert game.commands.index("rotate") > 0
    assert "observe" in game.commands[game.commands.index("rotate"):]
    assert sleeps
    assert job.execution.game_status == "started"


async def test_walk_always_schedules_stop():
    clock = FakeClock()

    async def sleep(seconds):
        clock.advance(seconds)

    game = ScriptedGame(states=[idle_state(), idle_state()])
    executor = GameExecutor(game, game, clock=clock, sleep=sleep)
    job = job_for("walk_forward")
    await executor.run_job(job)
    assert game.commands == ["observe", "walk_forward", "stop", "observe"]
    assert job.status == "completed"
    assert "1s" in (job.execution.interpretation or "") or "1" in (job.execution.interpretation or "")
    assert WALK_INTERPRETATION.split("{")[0] in job.execution.interpretation


async def test_wait_never_reaches_executor_game_commands():
    # Guard the executor allowlist: wait is not a game command.
    game = ScriptedGame()
    executor = GameExecutor(game, game)
    job = job_for("wait")
    await executor.run_job(job)
    assert "wait" not in game.commands
    assert job.status == "completed"


async def test_grab_requires_idle_empty_hands():
    game = ScriptedGame(states=[moving_state("walk_forward")])
    executor = GameExecutor(game, game)
    job = job_for("grab_item")
    await executor.run_job(job)
    assert job.status == "blocked"
    assert game.commands == ["observe"]


async def test_drop_reports_game_outcome():
    held = idle_state(held_item={"id": "1", "name": "crystal"})
    game = ScriptedGame(
        states=[held],
        acks=[GameAck(ok=False, status="drop_blocked", message="blocked", request_id="r", http_status=200)],
    )
    executor = GameExecutor(game, game)
    job = job_for("drop_item")
    job.scoring_state = deepcopy(held)
    await executor.run_job(job)
    assert job.status == "blocked"
    assert job.execution.game_status == "drop_blocked"
    assert game.commands == ["observe", "drop_item"]


async def test_dispatch_timeout_pauses_without_retry():
    game = ScriptedGame(errors={"rotate": GameClientError("timeout", timed_out=True, uncertain=True)})
    executor = GameExecutor(game, game)
    job = job_for("rotate")
    await executor.run_job(job)
    assert job.status == "execution_unknown"
    assert executor.paused
    assert game.commands.count("rotate") == 1
    follow = job_for("walk_forward")
    await executor.run_job(follow)
    assert follow.status == "blocked"
    assert follow.error == "execution_paused"


async def test_stale_decision_expires_instead_of_looping():
    clock = FakeClock(6)
    game = ScriptedGame()
    executor = GameExecutor(game, game, clock=clock)
    job = job_for("walk_forward", frame_started_at=0.0)
    await executor.run_job(job)
    assert job.status == "expired"
    assert game.commands == []


async def test_priority_stop_clears_then_stops_and_blocks_late_dispatch():
    game = ScriptedGame()
    executor = GameExecutor(game, game)
    result = await executor.priority_stop()
    assert result.game_cleared and result.game_stopped
    assert game.commands == ["clear_queue", "stop"]
    job = job_for("walk_forward")
    # Successful stop unpauses.
    await executor.run_job(job)
    assert "walk_forward" in game.commands


async def test_uncertain_stop_keeps_execution_paused():
    game = ScriptedGame(errors={"clear_queue": GameClientError("gone", uncertain=True, timed_out=True)})
    executor = GameExecutor(game, game)
    result = await executor.priority_stop()
    assert result.execution_paused
    assert result.game_stopped is False
    job = job_for("walk_forward")
    await executor.run_job(job)
    assert job.status == "blocked"


@pytest.mark.parametrize("action", ["walk_forward", "rotate", "grab_item", "drop_item"])
async def test_freshness_is_rechecked_after_pre_dispatch_observation(action):
    clock = FakeClock()
    state = idle_state(held_item={"id": "1", "name": "crystal"}) if action == "drop_item" else idle_state()

    class SlowObservationGame(ScriptedGame):
        async def observe(self):
            frame = await super().observe()
            clock.advance(6)
            return frame

    game = SlowObservationGame(states=[state])
    executor = GameExecutor(game, game, clock=clock)
    job = job_for(action, state=state, frame_started_at=0.0)
    await executor.run_job(job)
    assert job.status == "expired"
    assert job.error == "stale_decision"
    assert game.commands == ["observe"]


@pytest.mark.parametrize("invalidate", ["age", "stop"])
async def test_dispatch_rechecks_job_after_waiting_for_send_lock(invalidate):
    clock = FakeClock()
    observed = asyncio.Event()

    class ObservableGame(ScriptedGame):
        async def observe(self):
            frame = await super().observe()
            observed.set()
            return frame

    game = ObservableGame()
    executor = GameExecutor(game, game, clock=clock)
    generation = 0
    executor.bind(generation=lambda: generation, job_cancelled=lambda job: job.generation != generation)
    job = job_for("walk_forward", frame_started_at=0.0)
    await executor._send_lock.acquire()
    task = asyncio.create_task(executor.run_job(job))
    try:
        await asyncio.wait_for(observed.wait(), 1)
        if invalidate == "age":
            clock.advance(6)
        else:
            generation += 1
    finally:
        executor._send_lock.release()
        await asyncio.wait_for(task, 1)
    assert job.status == ("expired" if invalidate == "age" else "cancelled")
    assert game.commands == ["observe"]
