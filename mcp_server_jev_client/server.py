import asyncio
import logging
import math
import os
from time import perf_counter

import jev_interface
import mcp_server
import numpy as np
import voice
from voice_controller import (
    DirectGoalLease,
    GoalSupervisor,
    ReplanRequired,
)

MAX_AGENT_STEPS = int(os.environ.get("JEV_MAX_GOAL_STEPS", "10"))
MAX_AGENT_SECONDS = float(os.environ.get("JEV_MAX_GOAL_SECONDS", "60"))
ACTION_TIMEOUT_SECONDS = float(os.environ.get("GODOT_ACTION_TIMEOUT_SECONDS", "20"))
MIN_ACTION_CONFIDENCE = float(os.environ.get("JEV_MIN_ACTION_CONFIDENCE", "0.2"))


def _steering_delay_seconds() -> float:
    milliseconds = float(os.environ.get("VOICE_STEERING_DELAY_MS", "500"))
    if not math.isfinite(milliseconds) or milliseconds < 0:
        raise ValueError("VOICE_STEERING_DELAY_MS must be a finite non-negative number")
    return milliseconds / 1000


def _decision_state(world_state, step, max_steps, history):
    """Add compact loop progress without changing Godot's state dictionary."""
    state = dict(world_state)
    state["agent_progress"] = {
        "step": step,
        "max_steps": max_steps,
        # Snapshot the list so appending after the decision cannot rewrite the
        # progress object that was sent for this step.
        "completed_actions": list(history),
    }
    return state


def _remaining_seconds(instruction):
    if instruction.get("type") == "walk_forward":
        return instruction.get("remaining_seconds")
    if instruction.get("type") == "rotate":
        remaining = instruction.get("remaining_degrees")
        # The checked-in player default is 90 degrees/second. This estimate only
        # controls the next poll; the state itself determines completion.
        return abs(remaining) / 90 if isinstance(remaining, (int, float)) else None
    return None


async def _fresh_state(lease, pause_epoch):
    """Fetch state that was captured after the most recent recording pause."""
    while True:
        await lease.checkpoint(pause_epoch)
        pause_epoch = lease.pause_epoch
        state = await mcp_server.get_game_state()
        if not await lease.checkpoint(pause_epoch):
            return state, pause_epoch
        pause_epoch = lease.pause_epoch


async def _wait_for_actions(action_types, lease, pause_epoch):
    """Return fresh state after the selected movement types are no longer active."""
    deadline = asyncio.get_running_loop().time() + ACTION_TIMEOUT_SECONDS
    await asyncio.sleep(0.05)
    while True:
        if await lease.checkpoint(pause_epoch):
            raise ReplanRequired
        state = await mcp_server.get_game_state()
        if await lease.checkpoint(pause_epoch):
            raise ReplanRequired
        active = [item for item in state.get("game_state", {}).get("active_instructions", [])
                  if item.get("type") in action_types and item.get("status") == "running"]
        if not active:
            return state, pause_epoch
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Godot actions did not finish: {sorted(action_types)}")
        estimates = [_remaining_seconds(item) for item in active]
        estimates = [value for value in estimates if isinstance(value, (int, float)) and value > 0]
        await asyncio.sleep(min(max(estimates, default=0.2), 1.0))


def _active_instruction_types(world_state):
    return {
        instruction.get("type")
        for instruction in (world_state or {}).get("game_state", {}).get("active_instructions", [])
        if instruction.get("status") == "running"
    }


async def _execute_action(action, arguments, world_state=None, *,
                          lease=None, pause_epoch=0):
    """Dispatch one Jev choice, replacing only conflicting movement axes."""
    lease = lease or DirectGoalLease()
    movement_types = set()
    active = _active_instruction_types(world_state)
    started = perf_counter()

    async def operation():
        if action in {"walk_forward", "walk_and_turn"} and "walk_forward" in active:
            outcome = await mcp_server.cancel_walk()
            print(f"Godot targeted walk cancellation: {outcome['result']}")
        if action in {"rotate", "walk_and_turn"} and "rotate" in active:
            outcome = await mcp_server.cancel_rotation()
            print(f"Godot targeted rotation cancellation: {outcome['result']}")

        if action == "walk_and_turn":
            # The dispatch lock treats both submissions as one unit relative to
            # voice takeover. Godot still acknowledges each component separately.
            meters, degrees = arguments["meters"], arguments["degrees"]
            outcomes = await asyncio.gather(
                mcp_server.walk_forwards(meters), mcp_server.rotate(**degrees),
                return_exceptions=True,
            )
            results, failures = {}, []
            for name, outcome in zip(("walk_forward", "rotate"), outcomes):
                if isinstance(outcome, BaseException):
                    failures.append(name)
                    print(f"Godot {name} failed: {outcome}")
                else:
                    results[name] = outcome
                    print(f"Godot {name} result: {outcome['result']}")
            if failures:
                raise RuntimeError(
                    f"Combined movement incomplete: {', '.join(failures)} failed. "
                    "The other action may already be running; no automatic retry."
                )
            return {"ok": True, "result": results}
        if action == "walk_forward":
            return await mcp_server.walk_forwards(arguments["meters"])
        if action == "stop":
            return await mcp_server.stop_walking()
        if action == "rotate":
            return await mcp_server.rotate(**arguments["degrees"])
        if action == "cancel_walk":
            return await mcp_server.cancel_walk()
        if action == "cancel_rotation":
            return await mcp_server.cancel_rotation()
        if action == "grab_item":
            return await mcp_server.grab_item()
        if action == "drop_item":
            return await mcp_server.drop_item()
        if action == "interact":
            return await mcp_server.interact()
        raise ValueError(f"Unsupported Jev action: {action}")

    if action in {"walk_forward", "walk_and_turn"}:
        movement_types.add("walk_forward")
    if action in {"rotate", "walk_and_turn"}:
        movement_types.add("rotate")
    result = await lease.dispatch(operation, pause_epoch)
    print(f"Godot result: {result['result']}")
    logging.getLogger(__name__).info(
        "decision_action_ms=%.2f", (perf_counter() - started) * 1000
    )
    return result, movement_types


async def agent_send_execute_loop(interface, user_prompt="", world_prompt=None,
                                  *, max_steps=MAX_AGENT_STEPS, lease=None):
    """Replan from fresh Godot state until done, blocked, superseded, or bounded."""
    lease = lease or DirectGoalLease()
    started = asyncio.get_running_loop().time()
    pause_epoch = lease.pause_epoch
    if world_prompt is None:
        state, pause_epoch = await _fresh_state(lease, pause_epoch)
    else:
        state = world_prompt
    history = []
    step = 1

    while step <= max_steps:
        try:
            if await lease.checkpoint(pause_epoch):
                state, pause_epoch = await _fresh_state(lease, lease.pause_epoch)
            if asyncio.get_running_loop().time() - started >= MAX_AGENT_SECONDS:
                print(f"Goal stopped: {MAX_AGENT_SECONDS:g}s time limit reached.")
                return {"status": "time_limit", "history": history}

            decision = await interface.send_req(
                user_prompt, _decision_state(state, step, max_steps, history)
            )
            if await lease.checkpoint(pause_epoch):
                state, pause_epoch = await _fresh_state(lease, lease.pause_epoch)
                continue

            action = decision["action"]
            confidence = decision["confidence"]
            arguments = decision["arguments"]
            print(
                f"Jev step {step}: {action} "
                f"(confidence={confidence:.3f}) arguments={arguments}"
            )
            if confidence < MIN_ACTION_CONFIDENCE:
                print(f"Goal stopped: Jev confidence was below {MIN_ACTION_CONFIDENCE:g}.")
                return {"status": "low_confidence", "history": history}
            if action == "done":
                print(f"Goal complete after {step - 1} action(s).")
                return {"status": "done", "history": history}
            if action == "wait":
                print("Goal stopped: Jev reported that it is blocked or lacks evidence.")
                return {"status": "wait", "history": history}

            result, movement_types = await _execute_action(
                action, arguments, state, lease=lease, pause_epoch=pause_epoch
            )
            history.append({
                "action": action,
                "arguments": arguments,
                "godot_status": result.get("result"),
            })
            if step == max_steps:
                break
            if movement_types:
                state, pause_epoch = await _wait_for_actions(
                    movement_types, lease, pause_epoch
                )
            else:
                state, pause_epoch = await _fresh_state(lease, pause_epoch)
            step += 1
        except ReplanRequired:
            state, pause_epoch = await _fresh_state(lease, lease.pause_epoch)

    print(f"Goal stopped: {max_steps} step limit reached.")
    return {"status": "step_limit", "history": history}


async def voice_input_loop(supervisor, recorder, transcriber):
    """Own stdin and the recorder while goals execute independently."""
    while True:
        await asyncio.to_thread(input, "[Enter] record")
        token = supervisor.begin_recording()
        try:
            recorder.start()
            await asyncio.to_thread(input, "Listening... [Enter] stop ")
            samples = recorder.stop()
            user_text = await asyncio.to_thread(
                voice.process, samples, recorder, transcriber
            )
        except BaseException:
            supervisor.resume_after_invalid(token)
            raise

        if not user_text:
            supervisor.resume_after_invalid(token)
            continue
        print("USER TEXT:", user_text)
        supervisor.submit(user_text, token)


async def main():
    print("SETTING UP")
    interface = jev_interface.JevInterface()
    recorder = voice.Recorder()
    transcriber = voice.load_transcriber()
    transcriber(np.zeros(voice.TARGET_RATE // 2, dtype=np.float32))  # warm-up
    supervisor = GoalSupervisor(
        interface,
        agent_send_execute_loop,
        game=mcp_server,
        reaction_delay=_steering_delay_seconds(),
    )
    supervisor.start()

    print("SERVER LOOP STARTED")
    try:
        await voice_input_loop(supervisor, recorder, transcriber)
    finally:
        recorder.close()
        await supervisor.aclose(stop_game=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
