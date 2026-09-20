import asyncio
import time
import jev_interface
import mcp_server
import voice
import numpy as np

MAX_OBSERVE_DEPTH = 5

async def agent_send_execute_loop(interface, user_prompt="", world_prompt=""):
    for _ in range(MAX_OBSERVE_DEPTH):
        res = await interface.send_req(user_prompt, world_prompt)
        print("=========")
        print(interface.context)

        await mcp_server.rotate(
            int(res["x_dir"][0]), int(res["y_dir"][0]), int(res["z_dir"][0])
        )
        if res["movement_actions"][1] > 0.5:
            await mcp_server.walk_forwards(5)

        if res["observe_action"] > 0.5:
            world_prompt = await mcp_server.observe()
            user_prompt = ""
        else:
            break

import sys

def flush_stdin():
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except ImportError:  # Windows
        print("err")

started = False  # was `false`


async def execute_loop(interface):
    # THREAD 2, execute each mcp call in the queue
    last_sent = 0.0
    while True:
        if mcp_server.requests_queue.qsize() != 0:
            mcp_server.requests_queue.get()()
        if started and time.monotonic() - last_sent >= 1.0:
            last_sent = time.monotonic()
            # send world state every 1s
            world_state = await mcp_server.observe()
            await agent_send_execute_loop(interface, "", world_state)
        await asyncio.sleep(0.01)  # yield so the voice loop can run

async def voice_loop(interface, rec, transcriber):
    global started
    # THREAD 1
    print("SERVER LOOP STARTED")
    while True:
        flush_stdin()  # drop anything typed while we were busy

        await asyncio.to_thread(input, "[Enter] record")

        rec.start()
        answer = await asyncio.to_thread(input, "Listening... [Enter] stop ")
        samples = rec.stop()

        user_text = voice.process(samples, rec, transcriber)
        if not user_text:
            continue
        print("USER TEXT:", user_text)
        world_state = await mcp_server.observe()

        await agent_send_execute_loop(interface, user_text, world_state)
        if not started:  # was `if (started = False)`, a syntax error
            started = True

async def main():
    print("SETTING UP")
    interface = jev_interface.JevInterface()
    rec = voice.Recorder()
    transcriber = voice.load_transcriber()
    transcriber(np.zeros(voice.TARGET_RATE // 2, dtype=np.float32))  # warm-up

    try:
        await asyncio.gather(
            execute_loop(interface),
            voice_loop(interface, rec, transcriber),
        )
    finally:
        rec.close()

if __name__ == "__main__":
    asyncio.run(main())