"""Concurrent voice-goal supervision and race-safe Godot dispatch."""
from __future__ import annotations

import asyncio
import logging
import math
import string
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class GoalSuperseded(Exception):
    """The goal may no longer observe, decide, or dispatch actions."""


class ReplanRequired(Exception):
    """Recording paused this goal; its pre-pause decision must be discarded."""


class DirectGoalLease:
    """Compatibility lease for direct tests and one-shot callers."""

    pause_epoch = 0

    async def checkpoint(self, expected_pause_epoch: int) -> bool:
        return False

    async def dispatch(self, operation: Callable[[], Awaitable],
                       expected_pause_epoch: int):
        return await operation()


@dataclass(frozen=True)
class VoiceCommand:
    generation: int
    transcript: str


class GoalLease:
    """Generation-bound capabilities handed to one autonomous goal task."""

    def __init__(self, supervisor: "GoalSupervisor", generation: int):
        self._supervisor = supervisor
        self.generation = generation

    @property
    def pause_epoch(self) -> int:
        return self._supervisor.pause_epoch

    def _ensure_current(self) -> None:
        if self._supervisor.closed or self.generation != self._supervisor.generation:
            raise GoalSuperseded(f"goal {self.generation} was superseded")

    async def checkpoint(self, expected_pause_epoch: int) -> bool:
        self._ensure_current()
        await self._supervisor.planning_allowed.wait()
        self._ensure_current()
        return self.pause_epoch != expected_pause_epoch

    async def dispatch(self, operation: Callable[[], Awaitable],
                       expected_pause_epoch: int):
        """Serialize a complete action submission and settle it before cancellation."""
        self._ensure_current()
        await self._supervisor.planning_allowed.wait()
        self._ensure_current()
        async with self._supervisor.dispatch_lock:
            self._ensure_current()
            if not self._supervisor.planning_allowed.is_set():
                await self._supervisor.planning_allowed.wait()
                self._ensure_current()
            if self.pause_epoch != expected_pause_epoch:
                raise ReplanRequired

            request = asyncio.create_task(operation())
            try:
                return await asyncio.shield(request)
            except asyncio.CancelledError:
                # A local cancellation cannot undo a request Godot may already
                # have received. Keep the barrier locked until its outcome settles.
                try:
                    await asyncio.shield(request)
                except Exception:
                    logger.exception("Superseded Godot request failed while settling")
                raise


class GoalSupervisor:
    """Own one current goal while voice intake remains independently available."""

    HARD_STOP_PHRASES = frozenset({"stop", "halt", "cancel"})

    def __init__(self, interface, goal_runner, *, game, reaction_delay: float = 0.5):
        if not math.isfinite(reaction_delay) or reaction_delay < 0:
            raise ValueError("reaction_delay must be a finite non-negative number")
        self.interface = interface
        self.goal_runner = goal_runner
        self.game = game
        self.reaction_delay = reaction_delay
        self.generation = 0
        self.pause_epoch = 0
        self.planning_allowed = asyncio.Event()
        self.planning_allowed.set()
        self.dispatch_lock = asyncio.Lock()
        self._mailbox: asyncio.Queue[VoiceCommand] = asyncio.Queue(maxsize=1)
        self._runner_task: asyncio.Task | None = None
        self._active_goal: asyncio.Task | None = None
        self._pause_task: asyncio.Task | None = None
        self._hard_stop_task: asyncio.Task | None = None
        self._pending_hard_stop: VoiceCommand | None = None
        self._hard_stop_done = asyncio.Event()
        self._hard_stop_done.set()
        self._execution_blocked = False
        self._recording_token = 0
        self._paused_recording_token: int | None = None
        self._takeover_generation: int | None = None
        self.closed = False

    def start(self) -> None:
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(
                self._command_loop(), name="voice-goal-supervisor"
            )

    def begin_recording(self) -> int:
        """Schedule a human-like delayed pause and return this recording's token."""
        if self.closed:
            raise RuntimeError("goal supervisor is closed")
        self._recording_token += 1
        token = self._recording_token
        self._cancel_pause_timer()

        async def pause_later():
            try:
                await asyncio.sleep(self.reaction_delay)
                if token == self._recording_token and not self.closed:
                    self._paused_recording_token = token
                    self._pause_now(force_epoch=True)
                    print(f"Goal planning paused after {self.reaction_delay * 1000:.0f} ms.")
            except asyncio.CancelledError:
                pass

        self._pause_task = asyncio.create_task(
            pause_later(), name=f"voice-pause-{token}"
        )
        return token

    def resume_after_invalid(self, token: int) -> None:
        """Resume the same goal when a recording produced no usable transcript."""
        if token != self._recording_token or self.closed:
            return
        self._cancel_pause_timer()
        if self._paused_recording_token == token:
            self._paused_recording_token = None
        if self._allow_planning_if_ready():
            print("No valid steering command; previous goal resumed.")

    def submit(self, transcript: str, token: int) -> int:
        """Invalidate the old goal immediately and publish the newest transcript."""
        if self.closed:
            raise RuntimeError("goal supervisor is closed")
        if token != self._recording_token:
            raise ValueError("recording token is stale")
        transcript = transcript.strip()
        if not transcript:
            raise ValueError("transcript must not be empty")
        self._cancel_pause_timer()
        if self._paused_recording_token == token:
            self._paused_recording_token = None
        self.generation += 1
        self._pause_now()
        command = VoiceCommand(self.generation, transcript)
        if self._normalized(transcript) in self.HARD_STOP_PHRASES:
            self._discard_waiting_command()
            self._hard_stop_done.clear()
            # A newer stop must survive an in-progress attempt that may fail.
            # Multiple waiting stops can safely collapse to the newest request.
            self._pending_hard_stop = command
            if self._hard_stop_task is None or self._hard_stop_task.done():
                self._hard_stop_task = asyncio.create_task(
                    self._run_priority_stops(),
                    name=f"voice-hard-stop-{command.generation}",
                )
            return command.generation

        self._takeover_generation = self.generation
        self._publish_latest(command)
        return command.generation

    def _discard_waiting_command(self) -> None:
        try:
            discarded = self._mailbox.get_nowait()
        except asyncio.QueueEmpty:
            discarded = None
        if discarded is not None:
            self._mailbox.task_done()
            if self._takeover_generation == discarded.generation:
                self._takeover_generation = None
            print(f"Voice goal {discarded.generation} superseded before launch.")

    def _publish_latest(self, command: VoiceCommand) -> None:
        self._discard_waiting_command()
        self._mailbox.put_nowait(command)

    def _pause_now(self, *, force_epoch: bool = False) -> None:
        if force_epoch or self.planning_allowed.is_set():
            self.pause_epoch += 1
        self.planning_allowed.clear()

    def _allow_planning_if_ready(self) -> bool:
        if (not self.closed and self._paused_recording_token is None
                and self._takeover_generation is None
                and self._hard_stop_done.is_set()
                and not self._execution_blocked):
            was_paused = not self.planning_allowed.is_set()
            self.planning_allowed.set()
            return was_paused
        return False

    def _cancel_pause_timer(self) -> None:
        if self._pause_task is not None:
            self._pause_task.cancel()
            self._pause_task = None

    @staticmethod
    def _normalized(transcript: str) -> str:
        return transcript.strip().casefold().rstrip(string.whitespace + string.punctuation)

    async def _command_loop(self) -> None:
        while True:
            command = await self._mailbox.get()
            try:
                await self._hard_stop_done.wait()
                await self._replace_goal(command)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Voice takeover failed")
                print(f"Voice takeover failed: {error}. No replacement action was sent.")
            finally:
                if self._takeover_generation == command.generation:
                    self._takeover_generation = None
                self._allow_planning_if_ready()
                self._mailbox.task_done()

    async def _replace_goal(self, command: VoiceCommand) -> None:
        await self._cancel_active_goal()
        if command.generation != self.generation:
            return

        async with self.dispatch_lock:
            if command.generation != self.generation:
                return
            try:
                # After any uncertain barrier, require a confirmed global stop
                # before another autonomous goal is allowed to dispatch.
                await self._settled_barrier(self._execution_blocked)
            except Exception:
                self._execution_blocked = True
                raise
            self._execution_blocked = False

        if command.generation != self.generation:
            return
        state = await self.game.get_game_state()
        if command.generation != self.generation:
            return
        lease = GoalLease(self, command.generation)
        task = asyncio.create_task(
            self.goal_runner(
                self.interface, command.transcript, state, lease=lease
            ),
            name=f"voice-goal-{command.generation}",
        )
        self._active_goal = task
        task.add_done_callback(self._goal_finished)
        print(f"Voice goal {command.generation} started: {command.transcript}")

    async def _run_priority_stops(self) -> None:
        """Service every stop generation without exposing a gap to normal goals."""
        try:
            while self._pending_hard_stop is not None:
                command = self._pending_hard_stop
                self._pending_hard_stop = None
                try:
                    await self._run_priority_stop_attempt()
                except Exception as error:
                    self._execution_blocked = True
                    logger.exception(
                        "Voice hard stop could not establish its final barrier"
                    )
                    if self._pending_hard_stop is None:
                        print(
                            f"Voice hard stop uncertain: {error}. "
                            "Autonomous execution is blocked."
                        )
                    else:
                        logger.info("A newer hard stop is pending; retrying the barrier")
                else:
                    self._execution_blocked = False
                    print(f"Voice hard stop accepted: {command.transcript!r}.")
        except asyncio.CancelledError:
            if not self.closed:
                self._execution_blocked = True
            raise
        finally:
            self._hard_stop_done.set()
            self._hard_stop_task = None
            self._allow_planning_if_ready()

    async def _run_priority_stop_attempt(self) -> None:
        """Send a prompt best-effort stop, then establish an ordered barrier."""
        # This lane intentionally bypasses the action-dispatch lock. Start both
        # controls at once so a wedged clear request cannot delay the emergency
        # stop. The ordered barrier below provides the final safety guarantee.
        early_controls = {
            "stop": asyncio.create_task(self.game.stop_walking()),
            "clear_queue": asyncio.create_task(self.game.clear_queue()),
        }
        outcomes = await asyncio.gather(
            *early_controls.values(), return_exceptions=True
        )
        for name, outcome in zip(early_controls, outcomes):
            if isinstance(outcome, BaseException):
                logger.warning("Early hard-stop %s failed: %s", name, outcome)

        await self._cancel_active_goal()
        async with self.dispatch_lock:
            # An older shielded request may have landed after the early controls.
            # Ordered clear-then-stop closes that late-request race.
            await self._settled_barrier(True)

    async def _settled_barrier(self, hard_stop: bool) -> None:
        async def barrier():
            await self.game.clear_queue()
            if hard_stop:
                await self.game.stop_walking()

        task = asyncio.create_task(barrier())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                logger.exception("Godot interruption barrier failed while settling")
            raise

    def _goal_finished(self, task: asyncio.Task) -> None:
        if self._active_goal is task:
            self._active_goal = None
        if task.cancelled():
            return
        try:
            task.result()
        except GoalSuperseded:
            return
        except Exception:
            logger.exception("Voice goal failed")

    async def _cancel_active_goal(self) -> None:
        task = self._active_goal
        self._active_goal = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, GoalSuperseded):
            pass
        except Exception:
            # The failure was already consumed/logged by the completion callback;
            # it must not prevent a newer command from establishing its barrier.
            pass

    async def aclose(self, *, stop_game: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self._recording_token += 1
        self._paused_recording_token = None
        self._takeover_generation = None
        self._cancel_pause_timer()
        self.planning_allowed.set()
        await self._cancel_active_goal()
        hard_stop_task = self._hard_stop_task
        if hard_stop_task is not None and hard_stop_task is not asyncio.current_task():
            try:
                await hard_stop_task
            except asyncio.CancelledError:
                pass
        if self._runner_task is not None:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None
        if stop_game:
            try:
                async with self.dispatch_lock:
                    await self._settled_barrier(True)
            except Exception:
                logger.exception("Could not stop Godot during voice shutdown")
