import sys
import asyncio
import threading
from queue import Queue
import requests
import gemini_input
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("myserver")
gemini_vision = gemini_input.GeminiVision()
GAME_SERVER = "127.0.0.1:3000"
URL = f"http://{GAME_SERVER}/api/v1/commands"

requests_queue = Queue(maxsize=15)


def _post(payload):
    # timeout must exceed the game's 10s deadline
    return requests.post(URL, json=payload, timeout=15).json()


def _worker():
    # Single consumer: strict FIFO, one command at a time, waits for each response.
    while True:
        payload = requests_queue.get()
        try:
            body = _post(payload)
            result = body.get("result") or {}
            tag = "ok" if body.get("ok") else "FAILED"
            print(f"[queue] {payload['command']} {tag}: {result}", file=sys.stderr)
        except Exception as e:
            # no retry: a connection failure is ambiguous and a retried rotate turns twice
            print(f"[queue] {payload['command']} ERROR: {e!r}", file=sys.stderr)
        finally:
            requests_queue.task_done()  # required for join() in observe()


threading.Thread(target=_worker, daemon=True).start()


async def _enqueue(payload):
    # put() blocks when full; keep that off the event loop
    await asyncio.to_thread(requests_queue.put, payload)


@mcp.tool()
async def walk_forwards(meters: float) -> None:
    """
    Move the requested number of meters using the game timer; negative moves backward, zero does nothing.
    """
    await _enqueue({"command": "walk_forward", "arguments": {"meters": meters}})


@mcp.tool()
async def stop_walking() -> None:
    """
    Stop all active actions (walking and turning). Pending requests and the held item are kept.
    """
    await _enqueue({"command": "stop"})


async def observe() -> dict:
    """
    Return the game's observation state (position, rotation, held item, active instructions).
    """
    # wait until every queued action has been sent, so the observation reflects them
    await asyncio.to_thread(requests_queue.join)
    body = await asyncio.to_thread(_post, {"command": "observe"})
    if not body.get("ok"):
        print(f"[observe] FAILED: {body.get('result')}", file=sys.stderr)
    return body["result"]


@mcp.tool()
async def grab_item() -> None:
    """
    Pick up the nearest unobstructed item in front. Requires not walking and empty hands.
    """
    await _enqueue({"command": "grab_item"})


@mcp.tool()
async def drop_item() -> None:
    """
    Drop the held item. Requires not walking.
    """
    await _enqueue({"command": "drop_item"})


@mcp.tool()
async def rotate(x: int = 0, y: int = 90, z: int = 0) -> None:
    """
    Turn by a RELATIVE yaw: y degrees are added to the current heading (positive turns right).
    x and z must be 0.
    """
    await _enqueue({"command": "rotate", "arguments": {"degrees": {"x": x, "y": y, "z": z}}})


if __name__ == "__main__":
    mcp.run(transport="stdio")