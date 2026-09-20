"""Offline concurrency coverage for voice-goal interruption."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_controller import GoalSuperseded, GoalSupervisor


async def wait_until(predicate):
    """Yield deterministically until an asynchronous state transition occurs."""
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def fake_game(*, state=None):
    return SimpleNamespace(
        clear_queue=AsyncMock(return_value={"ok": True}),
        stop_walking=AsyncMock(return_value={"ok": True}),
        get_game_state=AsyncMock(return_value=state or {
            "game_state": {"active_instructions": []}, "vision": None,
        }),
    )


def submit(supervisor, transcript):
    token = supervisor.begin_recording()
    return supervisor.submit(transcript, token)


@pytest.mark.asyncio
async def test_recording_pause_is_delayed_and_invalid_recording_resumes_goal():
    supervisor = GoalSupervisor(
        object(), AsyncMock(), game=fake_game(), reaction_delay=0,
    )
    supervisor.start()
    try:
        token = supervisor.begin_recording()

        # Even a zero-delay pause is scheduled, so merely pressing record does
        # not synchronously halt the planner on the input call stack.
        assert supervisor.planning_allowed.is_set()
        await wait_until(lambda: not supervisor.planning_allowed.is_set())
        paused_epoch = supervisor.pause_epoch

        supervisor.resume_after_invalid(token)

        assert supervisor.planning_allowed.is_set()
        assert supervisor.pause_epoch == paused_epoch
    finally:
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_takeover_completion_does_not_override_a_new_recording_pause():
    barrier_started = asyncio.Event()
    release_barrier = asyncio.Event()

    async def clear_queue():
        barrier_started.set()
        await release_barrier.wait()
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    supervisor = GoalSupervisor(object(), AsyncMock(), game=game, reaction_delay=0)
    supervisor.start()
    try:
        submit(supervisor, "goal A")
        await asyncio.wait_for(barrier_started.wait(), 1)
        second_token = supervisor.begin_recording()
        await wait_until(lambda: supervisor._paused_recording_token == second_token)

        release_barrier.set()
        await supervisor._mailbox.join()

        assert not supervisor.planning_allowed.is_set()
        supervisor.resume_after_invalid(second_token)
        assert supervisor.planning_allowed.is_set()
    finally:
        release_barrier.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_latest_waiting_transcript_wins_while_takeover_is_blocked():
    first_barrier_started = asyncio.Event()
    release_first_barrier = asyncio.Event()
    clear_count = 0

    async def clear_queue():
        nonlocal clear_count
        clear_count += 1
        if clear_count == 1:
            first_barrier_started.set()
            await release_first_barrier.wait()
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    launched = []
    newest_started = asyncio.Event()

    async def run_goal(interface, transcript, state, *, lease):
        launched.append((transcript, lease.generation, state))
        if transcript == "goal C":
            newest_started.set()

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        submit(supervisor, "goal A")
        await first_barrier_started.wait()

        submit(supervisor, "goal B")
        newest_generation = submit(supervisor, "goal C")
        release_first_barrier.set()
        await asyncio.wait_for(newest_started.wait(), 1)

        assert [(text, generation) for text, generation, _ in launched] == [
            ("goal C", newest_generation)
        ]
        assert clear_count == 2  # stale A barrier, then the newest barrier
        game.get_game_state.assert_awaited_once()
        game.stop_walking.assert_not_awaited()
    finally:
        release_first_barrier.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_superseded_lease_cannot_dispatch_a_late_action():
    game = fake_game()
    leases = {}
    first_started = asyncio.Event()

    async def run_goal(interface, transcript, state, *, lease):
        leases[transcript] = lease
        if transcript == "goal A":
            first_started.set()

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        submit(supervisor, "goal A")
        await asyncio.wait_for(first_started.wait(), 1)
        old_lease = leases["goal A"]

        submit(supervisor, "goal B")
        stale_action = AsyncMock(return_value={"ok": True})

        with pytest.raises(GoalSuperseded):
            await old_lease.dispatch(stale_action, old_lease.pause_epoch)
        with pytest.raises(GoalSuperseded):
            await old_lease.checkpoint(old_lease.pause_epoch)
        stale_action.assert_not_awaited()
    finally:
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_takeover_waits_for_an_inflight_action_before_clearing_old_requests():
    game = fake_game()
    action_started = asyncio.Event()
    finish_action = asyncio.Event()
    replacement_started = asyncio.Event()
    ordering = []

    async def clear_queue():
        ordering.append("clear")
        return {"ok": True}

    game.clear_queue = clear_queue

    async def run_goal(interface, transcript, state, *, lease):
        if transcript == "goal B":
            replacement_started.set()
            return

        async def old_action():
            ordering.append("old_action_started")
            action_started.set()
            await finish_action.wait()
            ordering.append("old_action_finished")
            return {"ok": True}

        await lease.dispatch(old_action, lease.pause_epoch)

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        submit(supervisor, "goal A")
        await asyncio.wait_for(action_started.wait(), 1)
        ordering.clear()  # Ignore goal A's already-completed takeover barrier.

        submit(supervisor, "goal B")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert ordering == []

        finish_action.set()
        await asyncio.wait_for(replacement_started.wait(), 1)

        assert ordering == ["old_action_finished", "clear"]
    finally:
        finish_action.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_exact_stop_overtakes_queued_goals_and_cannot_be_dropped_by_a_new_goal():
    controls = []
    early_stop_sent = asyncio.Event()
    newest_started = asyncio.Event()

    async def clear_queue():
        controls.append("clear")
        return {"ok": True}

    async def stop_walking():
        controls.append("stop")
        early_stop_sent.set()
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    game.stop_walking = stop_walking
    launched = []

    async def run_goal(interface, transcript, state, *, lease):
        launched.append(transcript)
        if transcript == "goal C":
            newest_started.set()

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    await supervisor.dispatch_lock.acquire()
    test_holds_lock = True
    try:
        submit(supervisor, "goal A")
        await wait_until(lambda: supervisor._mailbox.empty())
        submit(supervisor, "goal B")  # waiting ordinary command
        submit(supervisor, "STOP")    # discards B and bypasses the held lock
        submit(supervisor, "goal C")  # must not replace/drop the priority stop

        await asyncio.wait_for(early_stop_sent.wait(), 1)
        assert controls == ["stop", "clear"]
        assert launched == []

        supervisor.dispatch_lock.release()
        test_holds_lock = False
        await asyncio.wait_for(newest_started.wait(), 1)

        # The final locked barrier completes before the newest ordinary goal.
        assert controls == ["stop", "clear", "clear", "stop", "clear"]
        assert launched == ["goal C"]
    finally:
        if test_holds_lock:
            supervisor.dispatch_lock.release()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_exact_stop_sends_early_controls_then_a_final_barrier_after_inflight_action():
    action_started = asyncio.Event()
    finish_action = asyncio.Event()
    early_stop_sent = asyncio.Event()
    controls = []
    stop_count = 0

    async def clear_queue():
        controls.append("clear")
        return {"ok": True}

    async def stop_walking():
        nonlocal stop_count
        stop_count += 1
        controls.append("stop")
        if stop_count == 1:
            early_stop_sent.set()
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    game.stop_walking = stop_walking

    async def run_goal(interface, transcript, state, *, lease):
        async def old_action():
            controls.append("old_action_started")
            action_started.set()
            await finish_action.wait()
            controls.append("old_action_finished")
            return {"ok": True}

        await lease.dispatch(old_action, lease.pause_epoch)

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        submit(supervisor, "goal A")
        await asyncio.wait_for(action_started.wait(), 1)
        controls.clear()  # Ignore A's initial takeover clear and action start.

        submit(supervisor, "stop")
        await asyncio.wait_for(early_stop_sent.wait(), 1)

        # Priority controls do not wait behind the dispatch lock held by A.
        assert controls == ["stop", "clear"]
        finish_action.set()
        await wait_until(lambda: supervisor._hard_stop_done.is_set())

        # Once A settles, the locked barrier closes the late-request race.
        assert controls == [
            "stop", "clear", "old_action_finished", "clear", "stop",
        ]
        assert supervisor.planning_allowed.is_set()
    finally:
        finish_action.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_exact_stop_bypasses_state_fetch_and_goal_runner():
    game = fake_game()
    goal_runner = AsyncMock()
    supervisor = GoalSupervisor(object(), goal_runner, game=game)
    supervisor.start()
    try:
        submit(supervisor, "  STOP!!!  ")
        await wait_until(lambda: game.stop_walking.await_count == 2)
        await wait_until(supervisor.planning_allowed.is_set)

        assert game.clear_queue.await_count == 2
        assert game.stop_walking.await_count == 2
        game.get_game_state.assert_not_awaited()
        goal_runner.assert_not_awaited()
        assert supervisor.planning_allowed.is_set()
    finally:
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_early_stop_is_not_delayed_by_a_hung_early_clear():
    release_clear = asyncio.Event()
    stop_sent = asyncio.Event()
    clear_count = 0

    async def clear_queue():
        nonlocal clear_count
        clear_count += 1
        if clear_count == 1:
            await release_clear.wait()
        return {"ok": True}

    async def stop_walking():
        stop_sent.set()
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    game.stop_walking = stop_walking
    supervisor = GoalSupervisor(object(), AsyncMock(), game=game)
    supervisor.start()
    try:
        submit(supervisor, "stop")
        await asyncio.wait_for(stop_sent.wait(), 1)

        assert clear_count == 1
        assert not supervisor._hard_stop_done.is_set()

        release_clear.set()
        await wait_until(supervisor._hard_stop_done.is_set)
        assert clear_count == 2
    finally:
        release_clear.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_newer_stop_retries_an_inflight_stop_attempt_that_fails():
    final_stop_started = asyncio.Event()
    release_failure = asyncio.Event()
    clear_count = 0
    stop_count = 0

    async def clear_queue():
        nonlocal clear_count
        clear_count += 1
        return {"ok": True}

    async def stop_walking():
        nonlocal stop_count
        stop_count += 1
        if stop_count == 2:
            final_stop_started.set()
            await release_failure.wait()
            raise RuntimeError("first final stop failed")
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    game.stop_walking = stop_walking
    supervisor = GoalSupervisor(object(), AsyncMock(), game=game)
    supervisor.start()
    try:
        submit(supervisor, "stop")
        await asyncio.wait_for(final_stop_started.wait(), 1)
        submit(supervisor, "halt")
        release_failure.set()
        await wait_until(supervisor._hard_stop_done.is_set)

        assert clear_count == 4
        assert stop_count == 4
        assert supervisor._execution_blocked is False
        assert supervisor.planning_allowed.is_set()
    finally:
        release_failure.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_unexpected_priority_stop_task_cancellation_fails_closed():
    control_started = asyncio.Event()
    hold_control = asyncio.Event()

    async def stop_walking():
        control_started.set()
        await hold_control.wait()
        return {"ok": True}

    game = fake_game()
    game.stop_walking = stop_walking
    supervisor = GoalSupervisor(object(), AsyncMock(), game=game)
    supervisor.start()
    try:
        submit(supervisor, "stop")
        await asyncio.wait_for(control_started.wait(), 1)
        hard_stop_task = supervisor._hard_stop_task
        hard_stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hard_stop_task

        assert supervisor._execution_blocked is True
        assert supervisor._hard_stop_done.is_set()
        assert not supervisor.planning_allowed.is_set()
    finally:
        hold_control.set()
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", [
    "turn left",       # compatible rotation while walking
    "walk backward",   # replace only the active walk in Godot
    "please stop",     # semantic stop still goes through Jev; only exact stop bypasses
])
async def test_non_exact_commands_preserve_active_movement_for_targeted_replacement(
    transcript,
):
    state = {"game_state": {"active_instructions": [
        {"type": "walk_forward", "status": "running", "remaining_seconds": 2.0},
    ]}, "vision": None}
    game = fake_game(state=state)
    received = []
    started = asyncio.Event()

    async def run_goal(interface, user_text, world_state, *, lease):
        received.append((user_text, world_state, lease.generation))
        started.set()

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        generation = submit(supervisor, transcript)
        await asyncio.wait_for(started.wait(), 1)

        assert received == [(transcript, state, generation)]
        game.clear_queue.assert_awaited_once_with()
        game.stop_walking.assert_not_awaited()
    finally:
        await supervisor.aclose(stop_game=False)


@pytest.mark.asyncio
async def test_failed_barrier_latches_execution_until_full_reconciliation():
    ordering = []
    first_failure_seen = asyncio.Event()

    async def clear_queue():
        ordering.append("clear")
        if ordering == ["clear"]:
            first_failure_seen.set()
            raise RuntimeError("clear failed")
        return {"ok": True}

    async def stop_walking():
        ordering.append("stop")
        return {"ok": True}

    game = fake_game()
    game.clear_queue = clear_queue
    game.stop_walking = stop_walking
    launched = []
    recovered = asyncio.Event()

    async def run_goal(interface, transcript, state, *, lease):
        ordering.append("plan")
        launched.append(transcript)
        recovered.set()

    supervisor = GoalSupervisor(object(), run_goal, game=game)
    supervisor.start()
    try:
        submit(supervisor, "turn left")
        await asyncio.wait_for(first_failure_seen.wait(), 1)
        await supervisor._mailbox.join()

        assert launched == []
        game.get_game_state.assert_not_awaited()
        assert supervisor._execution_blocked is True
        assert not supervisor.planning_allowed.is_set()

        submit(supervisor, "turn right")
        await asyncio.wait_for(recovered.wait(), 1)

        assert launched == ["turn right"]
        assert ordering == ["clear", "clear", "stop", "plan"]
        assert supervisor._execution_blocked is False
    finally:
        await supervisor.aclose(stop_game=False)
