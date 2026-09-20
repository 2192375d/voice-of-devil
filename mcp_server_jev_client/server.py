import asyncio
import logging
import os
from time import perf_counter
import jev_interface
import mcp_server
import voice
import numpy as np

MAX_AGENT_STEPS = int(os.environ.get("JEV_MAX_GOAL_STEPS", "10"))
MAX_AGENT_SECONDS = float(os.environ.get("JEV_MAX_GOAL_SECONDS", "60"))
ACTION_TIMEOUT_SECONDS = float(os.environ.get("GODOT_ACTION_TIMEOUT_SECONDS", "20"))
MIN_ACTION_CONFIDENCE = float(os.environ.get("JEV_MIN_ACTION_CONFIDENCE", "0.2"))


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


async def _wait_for_actions(action_types):
    """Return fresh state after the selected movement types are no longer active."""
    deadline = asyncio.get_running_loop().time() + ACTION_TIMEOUT_SECONDS
    await asyncio.sleep(0.05)
    while True:
        state = await mcp_server.get_game_state()
        active = [item for item in state.get("game_state", {}).get("active_instructions", [])
                  if item.get("type") in action_types and item.get("status") == "running"]
        if not active:
            return state
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Godot actions did not finish: {sorted(action_types)}")
        estimates = [_remaining_seconds(item) for item in active]
        estimates = [value for value in estimates if isinstance(value, (int, float)) and value > 0]
        await asyncio.sleep(min(max(estimates, default=0.2), 1.0))


async def _execute_action(action, arguments):
    """Dispatch one Jev choice and return its acknowledgment and movement types."""
    movement_types = set()
    started = perf_counter()
    if action == "walk_and_turn":
        # Godot acknowledges starting, not completion. Submit both without waiting
        # for either movement to finish. They are independent, not an atomic batch.
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
        result = {"ok": True, "result": results}
        movement_types = {"walk_forward", "rotate"}
    elif action == "walk_forward":
        result = await mcp_server.walk_forwards(arguments["meters"])
        movement_types = {"walk_forward"}
    elif action == "stop":
        result = await mcp_server.stop_walking()
    elif action == "rotate":
        result = await mcp_server.rotate(**arguments["degrees"])
        movement_types = {"rotate"}
    elif action == "grab_item":
        result = await mcp_server.grab_item()
    elif action == "drop_item":
        result = await mcp_server.drop_item()
    elif action == "interact":
        result = await mcp_server.interact()
    else:
        raise ValueError(f"Unsupported Jev action: {action}")
    print(f"Godot result: {result['result']}")
    logging.getLogger(__name__).info("decision_action_ms=%.2f", (perf_counter() - started) * 1000)
    return result, movement_types


async def agent_send_execute_loop(interface, user_prompt="", world_prompt=None,
                                  *, max_steps=MAX_AGENT_STEPS):
    """Replan from fresh Godot state until done, blocked, or safety-bounded."""
    started = asyncio.get_running_loop().time()
    state = world_prompt if world_prompt is not None else await mcp_server.get_game_state()
    history = []
    for step in range(1, max_steps + 1):
        if asyncio.get_running_loop().time() - started >= MAX_AGENT_SECONDS:
            print(f"Goal stopped: {MAX_AGENT_SECONDS:g}s time limit reached.")
            return {"status": "time_limit", "history": history}
        decision = await interface.send_req(
            user_prompt, _decision_state(state, step, max_steps, history)
        )
        action = decision["action"]
        confidence = decision["confidence"]
        arguments = decision["arguments"]
        print(f"Jev step {step}: {action} (confidence={confidence:.3f}) arguments={arguments}")
        if confidence < MIN_ACTION_CONFIDENCE:
            print(f"Goal stopped: Jev confidence was below {MIN_ACTION_CONFIDENCE:g}.")
            return {"status": "low_confidence", "history": history}
        if action == "done":
            print(f"Goal complete after {step - 1} action(s).")
            return {"status": "done", "history": history}
        if action == "wait":
            print("Goal stopped: Jev reported that it is blocked or lacks evidence.")
            return {"status": "wait", "history": history}

        result, movement_types = await _execute_action(action, arguments)
        history.append({"action": action, "arguments": arguments,
                        "godot_status": result.get("result")})
        if step == max_steps:
            break
        state = (await _wait_for_actions(movement_types) if movement_types
                 else await mcp_server.get_game_state())

    print(f"Goal stopped: {max_steps} step limit reached.")
    return {"status": "step_limit", "history": history}

import sys

def flush_stdin():
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except ImportError:  # Windows
        print("err")

async def main():
    print("SETTING UP")
    interface = jev_interface.JevInterface()
    rec = voice.Recorder()
    transcriber = voice.load_transcriber()
    transcriber(np.zeros(voice.TARGET_RATE // 2, dtype=np.float32))  # warm-up

    print("SERVER LOOP STARTED")
    try:
        while True:
            
            flush_stdin()  # drop anything typed while we were busy
            
            await asyncio.to_thread(input, "[Enter] record")

            rec.start()
            answer = await asyncio.to_thread(input, "Listening... [Enter] stop ")
            samples = rec.stop()

            # Keep local transcription off the event loop.
            user_text = await asyncio.to_thread(voice.process, samples, rec, transcriber)
            if not user_text:
                continue
            print("USER TEXT:", user_text)
            decision_started = perf_counter()
            try:
                world_state = await mcp_server.get_game_state()
                await agent_send_execute_loop(interface, user_text, world_state)
            except Exception as error:
                # Never replay a command after a timeout: it may already have executed.
                logging.getLogger(__name__).exception("Command cycle failed")
                print(f"Command failed: {error} No automatic retry.")
                continue
            logging.getLogger(__name__).info("transcript_to_action_loop_ms=%.2f",
                                             (perf_counter() - decision_started) * 1000)
    finally:
        rec.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
