"""Bounded FIFO of voice-command jobs and the scoring worker."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from time import monotonic, perf_counter
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from .models import (
    CommandContext,
    ExecutionResult,
    JobStatus,
    StopGameResult,
    VoiceCommandReceipt,
    VoiceCommandStatus,
)
from .providers import dispatch_eligibility

TERMINAL: frozenset[str] = frozenset({
    "completed",
    "needs_clarification",
    "blocked",
    "expired",
    "cancelled",
    "failed",
    "execution_unknown",
})
EXPLICIT_STOP_UTTERANCES = frozenset({"stop", "stop moving", "cancel all actions"})
QUEUE_CAPACITY = 16
QUEUE_WAIT_SECONDS = 30.0
RESULT_CACHE_SIZE = 100
RESULT_TTL_SECONDS = 600.0


def is_explicit_stop(transcript: str) -> bool:
    return transcript.strip().casefold() in EXPLICIT_STOP_UTTERANCES


@dataclass
class CommandJob:
    command_id: UUID
    transcript: str
    queued_at: float
    status: JobStatus = "queued"
    generation: int = 0
    source: str = "jev"
    scored: Any = None
    observation_sequence: int | None = None
    frame_started_at: float | None = None
    scoring_state: dict[str, Any] | None = None
    execution: ExecutionResult | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    finished_at: float | None = None


@dataclass
class CommandService:
    observer: Any
    vision: Any
    jev: Any
    executor: Any
    pipeline_lock: asyncio.Lock
    score_timeout: float = 60.0
    queue_capacity: int = QUEUE_CAPACITY
    queue_wait_seconds: float = QUEUE_WAIT_SECONDS
    result_cache_size: int = RESULT_CACHE_SIZE
    result_ttl_seconds: float = RESULT_TTL_SECONDS
    dispatch: bool = True
    clock: Callable[[], float] = monotonic
    observe_and_summarize: Callable[..., Awaitable[Any]] | None = None
    instance_id: UUID = field(default_factory=uuid4)
    generation: int = 0
    _jobs: dict[UUID, CommandJob] = field(default_factory=dict)
    _terminal_order: deque[UUID] = field(default_factory=deque)
    _pending: deque[CommandJob] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _worker: asyncio.Task | None = None
    _available: asyncio.Event = field(default_factory=asyncio.Event)

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="voice-command-worker")

    async def aclose(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        await self._cancel_pending("service_shutdown")
        await self.executor.shutdown()

    async def context(self) -> CommandContext:
        async with self._lock:
            return CommandContext(service_instance_id=self.instance_id, generation=self.generation)

    async def submit(
        self, command_id: UUID, transcript: str, *, context: CommandContext | None = None,
    ) -> VoiceCommandReceipt:
        text = transcript.strip()
        if not text:
            return VoiceCommandReceipt(
                accepted=False,
                service_instance_id=self.instance_id,
                command_id=command_id,
                error="empty_transcript",
            )
        async with self._lock:
            # MCP callers must supply a context. Trusted in-process callers may
            # admit directly. Check and enqueue under the same stop barrier.
            if context is not None and (
                context.service_instance_id != self.instance_id
                or context.generation != self.generation
            ):
                return VoiceCommandReceipt(
                    accepted=False, service_instance_id=self.instance_id,
                    command_id=command_id, error="stale_command_context",
                )
            existing = self._jobs.get(command_id)
            if existing is not None:
                if existing.transcript == text:
                    return VoiceCommandReceipt(
                        accepted=True,
                        service_instance_id=self.instance_id,
                        command_id=command_id,
                        status=existing.status,
                        existing=True,
                    )
                return VoiceCommandReceipt(
                    accepted=False,
                    service_instance_id=self.instance_id,
                    command_id=command_id,
                    error="payload_conflict",
                )
            if is_explicit_stop(text):
                job = CommandJob(
                    command_id=command_id,
                    transcript=text,
                    queued_at=self.clock(),
                    status="dispatching",
                    generation=self.generation,
                    source="explicit_control",
                )
                self._jobs[command_id] = job
            else:
                queued = sum(1 for item in self._jobs.values() if item.status == "queued")
                if queued >= self.queue_capacity:
                    return VoiceCommandReceipt(
                        accepted=False,
                        service_instance_id=self.instance_id,
                        command_id=command_id,
                        error="queue_full",
                    )
                job = CommandJob(
                    command_id=command_id,
                    transcript=text,
                    queued_at=self.clock(),
                    generation=self.generation,
                )
                self._jobs[command_id] = job
                self._pending.append(job)
                self._available.set()
        if is_explicit_stop(text):
            await self._stop_job(job, "Priority stop from an exact reserved utterance.")
        return VoiceCommandReceipt(
            accepted=True, service_instance_id=self.instance_id,
            command_id=command_id, status=job.status,
        )

    async def _stop_job(self, job: CommandJob, interpretation: str) -> None:
        # The stop job is already dispatching, so it survives cancellation of
        # queued/scoring work. Both spoken and scored stops use this path.
        result = await self.stop_game()
        async with self._lock:
            job.execution = ExecutionResult(
                game_status="stopped" if result.game_stopped else None,
                message=result.error, paused=result.execution_paused,
                interpretation=interpretation,
            )
            job.status = (
                "execution_unknown" if result.execution_paused or not result.game_stopped
                else "completed"
            )
            self._finish(job)

    async def get(self, command_id: UUID) -> VoiceCommandStatus:
        async with self._lock:
            job = self._jobs.get(command_id)
            if job is None:
                return VoiceCommandStatus(
                    found=False,
                    service_instance_id=self.instance_id,
                    command_id=command_id,
                    error="unknown_command",
                )
            return self._view(job)

    async def cancel(self, command_id: UUID) -> VoiceCommandStatus:
        async with self._lock:
            job = self._jobs.get(command_id)
            if job is None:
                return VoiceCommandStatus(
                    found=False,
                    service_instance_id=self.instance_id,
                    command_id=command_id,
                    error="unknown_command",
                )
            if job.status in ("dispatching", "dispatched") or job.status in TERMINAL:
                return self._view(job)
            job.status = "cancelled"
            job.error = "cancelled"
            self._finish(job)
            return self._view(job)

    async def stop_game(self) -> StopGameResult:
        async with self._lock:
            self.generation += 1
            cancelled = 0
            for job in list(self._jobs.values()):
                if job.source == "explicit_control" and job.status == "dispatching":
                    job.generation = self.generation
                    continue
                if job.status in ("queued", "scoring", "ready_to_execute"):
                    job.status = "cancelled"
                    job.error = "priority_stop"
                    job.generation = self.generation
                    self._finish(job)
                    cancelled += 1
        result = await self.executor.priority_stop()
        return StopGameResult(
            service_instance_id=self.instance_id,
            cancelled_jobs=cancelled,
            generation=self.generation,
            game_cleared=result.game_cleared,
            game_stopped=result.game_stopped,
            execution_paused=result.execution_paused,
            error=result.error,
        )

    def _view(self, job: CommandJob) -> VoiceCommandStatus:
        return VoiceCommandStatus(
            found=True,
            service_instance_id=self.instance_id,
            command_id=job.command_id,
            transcript=job.transcript,
            status=job.status,
            source=job.source,  # type: ignore[arg-type]
            decision=job.scored,
            observation_sequence=job.observation_sequence,
            execution=job.execution,
            timings_ms=dict(job.timings_ms),
            error=job.error,
        )

    def _finish(self, job: CommandJob) -> None:
        if job.status not in TERMINAL:
            return
        job.finished_at = self.clock()
        if job.command_id not in self._terminal_order:
            self._terminal_order.append(job.command_id)
        self._prune()

    def _prune(self) -> None:
        now = self.clock()
        while self._terminal_order:
            oldest = self._jobs.get(self._terminal_order[0])
            if oldest is None:
                self._terminal_order.popleft()
                continue
            if oldest.status not in TERMINAL:
                self._terminal_order.popleft()
                continue
            aged = now - (oldest.finished_at or oldest.queued_at) > self.result_ttl_seconds
            over = len(self._terminal_order) > self.result_cache_size
            if aged or over:
                self._terminal_order.popleft()
                self._jobs.pop(oldest.command_id, None)
            else:
                break

    async def _cancel_pending(self, reason: str) -> None:
        async with self._lock:
            self.generation += 1
            for job in list(self._jobs.values()):
                if job.status in ("queued", "scoring", "ready_to_execute"):
                    job.status = "cancelled"
                    job.error = reason
                    job.generation = self.generation
                    self._finish(job)

    async def _run(self) -> None:
        try:
            while True:
                job = await self._take()
                await self._handle(job)
        except asyncio.CancelledError:
            raise

    async def _take(self) -> CommandJob:
        while True:
            async with self._lock:
                while self._pending and self._pending[0].status == "cancelled":
                    self._pending.popleft()
                if self._pending:
                    return self._pending.popleft()
                self._available.clear()
            await self._available.wait()

    async def _handle(self, job: CommandJob) -> None:
        if job.status == "cancelled":
            return
        waited = self.clock() - job.queued_at
        job.timings_ms["queue_wait"] = waited * 1000
        if waited > self.queue_wait_seconds:
            async with self._lock:
                if job.status == "cancelled":
                    return
                job.status = "expired"
                job.error = "queue_wait_exceeded"
                self._finish(job)
            return
        async with self._lock:
            if job.status == "cancelled":
                return
            job.status = "scoring"
        try:
            async with self.pipeline_lock:
                async with self._lock:
                    if job.status == "cancelled":
                        return
                async with asyncio.timeout(self.score_timeout):
                    # Conservative source-frame age includes capture and vision,
                    # not just the time spent asking Jev.
                    job.frame_started_at = self.clock()
                    frame, observation, timings = await self.observe_and_summarize(
                        self.observer, self.vision,
                    )
                    job.observation_sequence = frame.state["observation_sequence"]
                    job.scoring_state = frame.state
                    job.timings_ms.update(timings)
                    tick = perf_counter()
                    scored = await self.jev.score(job.transcript, frame.state, observation)
                    job.timings_ms["jev"] = (perf_counter() - tick) * 1000
        except Exception:
            async with self._lock:
                if job.status != "cancelled":
                    job.status = "failed"
                    job.error = "scoring_failed"
                    self._finish(job)
            return
        async with self._lock:
            if job.status == "cancelled":
                return
            job.scored = scored
            if dispatch_eligibility(scored) != "dispatch":
                job.status = "needs_clarification"
                self._finish(job)
                return
            if scored.action == "wait" or not self.dispatch:
                job.status = "completed"
                job.execution = ExecutionResult(
                    interpretation=(
                        "No game command; wait is local."
                        if scored.action == "wait"
                        else "Score-only; dispatch disabled."
                    ),
                )
                self._finish(job)
                return
            if self.executor.paused and scored.action != "stop":
                job.status = "blocked"
                job.error = "execution_paused"
                self._finish(job)
                return
            job.status = "dispatching" if scored.action == "stop" else "ready_to_execute"
        if scored.action == "stop":
            await self._stop_job(job, "Priority stop for a scored stop action.")
            return
        await self.executor.run_job(job)
        async with self._lock:
            self._finish(job)
