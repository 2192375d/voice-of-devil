import asyncio
import logging
from time import perf_counter
import jev_interface
import mcp_server
import voice
import numpy as np

async def agent_send_execute_loop(interface, user_prompt="", world_prompt=""):
    """One decision: a single action or overlapping walking and turning."""
    started = perf_counter()
    decision = await interface.send_req(user_prompt, world_prompt)
    action = decision["action"]
    confidence = decision["confidence"]
    arguments = decision["arguments"]
    print(f"Jev action: {action} (confidence={confidence:.3f}) arguments={arguments}")
    if confidence <= 0.5 or action == "wait":
        print("No action sent: Jev chose wait or confidence was too low.")
        return
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
    elif action == "walk_forward":
        result = await mcp_server.walk_forwards(arguments["meters"])
    elif action == "stop":
        result = await mcp_server.stop_walking()
    elif action == "rotate":
        result = await mcp_server.rotate(**arguments["degrees"])
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
    return result

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
