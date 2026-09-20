"""Allowlisted game dispatch, completion waits, and priority stop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Awaitable, Callable

from .game_client import GameAck, GameClientError, GameFrame, rotate_arguments
from .models import ExecutionResult, ScoredDecision

WALK_INTERPRETATION = (
    "Bounded forward step of {seconds:g}s, then stop. The game walks indefinitely; "
    "this is not distance control and does not honor spoken durations."
)

@dataclass
class StopOutcome:
    game_cleared: bool = False
    game_stopped: bool = False
    execution_paused: bool = False
    error: str | None = None

@dataclass
class ExecutionPolicy:
    max_frame_age_seconds: float = 5.0
    walk_step_seconds: float = 1.0
    rotate_deadline_seconds: float = 8.0
    rotate_poll_seconds: float = 0.05
    idle_deadline_seconds: float = 2.0
    position_epsilon: float = 0.05
    yaw_epsilon: float = 2.0


@dataclass
class NullExecutor:
    paused: bool = False

    async def run_job(self, job: Any) -> None:
        job.status = "failed"
        job.error = "no_executor"

    async def priority_stop(self) -> StopOutcome:
        return StopOutcome(error="no_executor")

    async def shutdown(self) -> None:
        return None


def running_types(state: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    for instruction in state.get("active_instructions") or []:
        if instruction.get("status") == "running" and isinstance(instruction.get("type"), str):
            types.add(instruction["type"])
    return types


def is_idle(state: dict[str, Any]) -> bool:
    return not (running_types(state) & {"walk_forward", "rotate"})


def held_id(state: dict[str, Any]) -> Any:
    item = state.get("held_item")
    if not isinstance(item, dict):
        return None
    return item.get("id")


def _vector(state: dict[str, Any], key: str) -> dict[str, float]:
    value = state.get(key)
    return value if isinstance(value, dict) else {}


def _changed(left: dict[str, Any], right: dict[str, Any], epsilon: float) -> bool:
    for axis in ("x", "y", "z"):
        try:
            if abs(float(left.get(axis, 0)) - float(right.get(axis, 0))) > epsilon:
                return True
        except (TypeError, ValueError):
            return True
    return False


def preconditions_hold(action: str, scored_state: dict[str, Any], live: dict[str, Any], policy: ExecutionPolicy) -> bool:
    if action == "grab_item":
        if not is_idle(live) or held_id(live) is not None:
            return False
        if held_id(scored_state) is not None:
            return False
        if _changed(_vector(scored_state, "position"), _vector(live, "position"), policy.position_epsilon):
            return False
        if _changed(
            _vector(scored_state, "rotation_degrees"),
            _vector(live, "rotation_degrees"),
            policy.yaw_epsilon,
        ):
            return False
        return True
    if action == "drop_item":
        if not is_idle(live) or held_id(live) is None:
            return False
        if held_id(scored_state) != held_id(live):
            return False
        if _changed(_vector(scored_state, "position"), _vector(live, "position"), policy.position_epsilon):
            return False
        return True
    if action == "walk_forward":
        return is_idle(live)
    if action == "rotate":
        return "rotate" not in running_types(live)
    if action == "stop":
        return True
    return False


@dataclass
class GameExecutor:
    transport: Any
    observer: Any
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    paused: bool = False
    owns_movement: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _generation: Callable[[], int] | None = None
    _job_cancelled: Callable[[Any], bool] | None = None

    def bind(self, *, generation: Callable[[], int], job_cancelled: Callable[[Any], bool]) -> None:
        self._generation = generation
        self._job_cancelled = job_cancelled

    def _cancelled(self, job: Any) -> bool:
        if job.status == "cancelled":
            return True
        if self._job_cancelled is not None:
            return self._job_cancelled(job)
        return False

    async def shutdown(self) -> None:
        if self.owns_movement:
            await self.priority_stop()

    async def priority_stop(self) -> StopOutcome:
        async with self._send_lock:
            try:
                cleared = await self.transport.execute("clear_queue")
            except GameClientError as error:
                self.paused = True
                return StopOutcome(execution_paused=True, error="clear_queue_uncertain" if error.uncertain else "clear_queue_failed")
            if not cleared.ok:
                self.paused = True
                return StopOutcome(execution_paused=True, error=cleared.status)
            try:
                stopped = await self.transport.execute("stop")
            except GameClientError as error:
                self.paused = True
                return StopOutcome(
                    game_cleared=True,
                    execution_paused=True,
                    error="stop_uncertain" if error.uncertain else "stop_failed",
                )
            if not stopped.ok:
                self.paused = True
                return StopOutcome(game_cleared=True, execution_paused=True, error=stopped.status)
            self.paused = False
            self.owns_movement = False
            return StopOutcome(game_cleared=True, game_stopped=True)

    async def run_job(self, job: Any) -> None:
        scored: ScoredDecision = job.scored
        if scored.action == "wait":
            job.status = "completed"
            job.execution = ExecutionResult(interpretation="No game command; wait is local.")
            return
        if scored.action == "stop":
            job.status = "dispatching"
            result = await self.priority_stop()
            job.execution = ExecutionResult(
                game_status="stopped" if result.game_stopped else None,
                message=result.error,
                paused=result.execution_paused,
                interpretation="Priority stop for a scored stop action.",
            )
            job.status = "execution_unknown" if result.execution_paused or not result.game_stopped else "completed"
            return
        if self.paused:
            job.status = "blocked"
            job.error = "execution_paused"
            return
        if self._expired(job):
            return
        try:
            live: GameFrame = await self.observer.observe()
        except GameClientError as error:
            job.status = "failed"
            job.error = "pre_dispatch_observe_timeout" if error.timed_out else "pre_dispatch_observe_failed"
            return
        if self._cancelled(job):
            job.status = "cancelled"
            return
        if not preconditions_hold(scored.action, job.scoring_state or {}, live.state, self.policy):
            job.status = "blocked"
            job.error = "precondition_changed"
            job.observation_sequence = live.state.get("observation_sequence", job.observation_sequence)
            return
        if scored.action == "walk_forward":
            await self._walk(job)
        elif scored.action == "rotate":
            await self._rotate(job, scored)
        elif scored.action in {"grab_item", "drop_item"}:
            await self._instant(job, scored.action)
        else:
            job.status = "failed"
            job.error = "unsupported_action"

    def _expired(self, job: Any) -> bool:
        if (job.frame_started_at is not None
                and self.clock() - job.frame_started_at > self.policy.max_frame_age_seconds):
            job.status = "expired"
            job.error = "stale_decision"
            return True
        return False

    async def _send_job(
        self, job: Any, command: str, arguments: dict[str, Any] | None = None,
    ) -> GameAck | None:
        async with self._send_lock:
            # Recheck after both the fresh observation and any wait for another
            # sender. No await between these checks and initiating the request.
            if self._cancelled(job):
                job.status = "cancelled"
                return None
            if self._expired(job):
                return None
            if self.paused:
                job.status = "blocked"
                job.error = "execution_paused"
                return None
            job.status = "dispatching"
            return await self.transport.execute(command, arguments)

    async def _send(self, command: str, arguments: dict[str, Any] | None = None) -> GameAck:
        async with self._send_lock:
            if self.paused and command not in {"clear_queue", "stop"}:
                raise GameClientError("execution is paused", status="execution_paused")
            return await self.transport.execute(command, arguments)

    async def _walk(self, job: Any) -> None:
        try:
            ack = await self._send_job(job, "walk_forward")
        except GameClientError as error:
            return self._uncertain(job, error)
        if ack is None:
            return
        if not ack.ok or ack.status != "started":
            job.status = "blocked"
            job.execution = ExecutionResult(game_status=ack.status, message=ack.message, request_id=ack.request_id)
            job.error = ack.status
            return
        job.status = "dispatched"
        job.execution = ExecutionResult(
            game_status=ack.status,
            request_id=ack.request_id,
            interpretation=WALK_INTERPRETATION.format(seconds=self.policy.walk_step_seconds),
        )
        self.owns_movement = True
        deadline = self.clock() + self.policy.walk_step_seconds
        while self.clock() < deadline:
            if self._cancelled(job) or self.paused:
                job.status = "cancelled"
                return
            remaining = deadline - self.clock()
            await self.sleep(min(self.policy.rotate_poll_seconds, remaining))
        if self._cancelled(job) or self.paused:
            job.status = "cancelled"
            return
        try:
            stop = await self._send("stop")
        except GameClientError as error:
            return self._uncertain(job, error)
        if not stop.ok:
            self.paused = True
            job.status = "execution_unknown"
            job.execution = ExecutionResult(
                game_status=stop.status,
                message=stop.message,
                request_id=stop.request_id,
                paused=True,
                interpretation=job.execution.interpretation if job.execution else None,
            )
            return
        if not await self._wait_idle(job, self.policy.idle_deadline_seconds):
            return
        self.owns_movement = False
        job.status = "completed"

    async def _rotate(self, job: Any, scored: ScoredDecision) -> None:
        yaw = scored.arguments.degrees.y
        try:
            ack = await self._send_job(job, "rotate", rotate_arguments(yaw))
        except GameClientError as error:
            return self._uncertain(job, error)
        if ack is None:
            return
        if not ack.ok:
            job.status = "blocked"
            job.execution = ExecutionResult(game_status=ack.status, message=ack.message, request_id=ack.request_id)
            job.error = ack.status
            return
        job.status = "dispatched"
        job.execution = ExecutionResult(game_status=ack.status, request_id=ack.request_id)
        if ack.status == "started" and yaw != 0:
            self.owns_movement = True
            deadline = self.clock() + self.policy.rotate_deadline_seconds
            while self.clock() < deadline:
                if self._cancelled(job) or self.paused:
                    job.status = "cancelled"
                    return
                try:
                    frame = await self.observer.observe()
                except GameClientError as error:
                    job.status = "execution_unknown"
                    job.error = "rotate_observe_timeout" if error.timed_out else "rotate_observe_failed"
                    self.paused = True
                    return
                if "rotate" not in running_types(frame.state):
                    self.owns_movement = False
                    job.status = "completed"
                    return
                await self.sleep(self.policy.rotate_poll_seconds)
            job.status = "execution_unknown"
            job.error = "rotate_deadline"
            self.paused = True
            return
        job.status = "completed"

    async def _instant(self, job: Any, command: str) -> None:
        try:
            ack = await self._send_job(job, command)
        except GameClientError as error:
            return self._uncertain(job, error)
        if ack is None:
            return
        job.execution = ExecutionResult(
            game_status=ack.status, message=ack.message, request_id=ack.request_id,
        )
        if ack.ok and ack.status in {"picked_up", "dropped", "started"}:
            job.status = "completed"
        else:
            job.status = "blocked"
            job.error = ack.status

    async def _wait_idle(self, job: Any, deadline_seconds: float) -> bool:
        deadline = self.clock() + deadline_seconds
        while self.clock() < deadline:
            if self._cancelled(job):
                job.status = "cancelled"
                return False
            try:
                frame = await self.observer.observe()
            except GameClientError as error:
                self._uncertain(job, error)
                return False
            if is_idle(frame.state):
                return True
            await self.sleep(self.policy.rotate_poll_seconds)
        job.status = "execution_unknown"
        job.error = "idle_deadline"
        self.paused = True
        return False

    def _uncertain(self, job: Any, error: GameClientError) -> None:
        self.paused = True
        job.status = "execution_unknown"
        job.error = "dispatch_timeout" if error.timed_out else "dispatch_failed"
        job.execution = ExecutionResult(paused=True, message=error.status)
