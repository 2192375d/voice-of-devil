"""Goal-independent observation pipeline, shared by MCP and the benchmark."""
import logging
from time import perf_counter

import httpx

logger = logging.getLogger(__name__)


async def fetch_frame(client: httpx.AsyncClient, game_server: str) -> dict:
    response = await client.post(
        f"http://{game_server}/api/v1/commands", json={"command": "observe"}
    )
    response.raise_for_status()
    frame = response.json()
    if not isinstance(frame, dict) or frame.get("ok") is not True:
        raise ValueError("Godot observation failed")
    state, image = frame.get("result"), frame.get("image")
    if not isinstance(state, dict) or state.get("status") != "observed":
        raise ValueError("Godot did not return an observation")
    if (type(state.get("observation_sequence")) is not int
            or not isinstance(state.get("simulation_time"), (float, int))):
        raise ValueError("Godot observation is missing its sequence or simulation time")
    if (not isinstance(image, dict) or image.get("mime_type") != "image/png"
            or not isinstance(image.get("data"), str) or not image["data"]):
        raise ValueError("Godot did not return a PNG image")
    return frame


async def observe_world(client: httpx.AsyncClient, game_server: str, vision) -> dict:
    start = perf_counter()
    frame = await fetch_frame(client, game_server)
    captured = perf_counter()
    state = frame["result"]
    sequence = state["observation_sequence"]
    description = await vision.summarize(
        frame["image"]["data"], hints=state.get("hints"), observation_sequence=sequence
    )
    finished = perf_counter()
    logger.info("observe sequence=%s game_ms=%.2f gemini_ms=%.2f total_ms=%.2f",
                sequence, (captured - start) * 1000, (finished - captured) * 1000,
                (finished - start) * 1000)
    return {
        "observation_sequence": sequence,
        "simulation_time": state["simulation_time"],
        "game_state": {key: value for key, value in state.items()
                       if key not in {"status", "message", "observation_sequence", "simulation_time", "hints"}},
        "vision": {
            "source": "gemini",
            "source_observation_sequence": sequence,
            **description.model_dump(),
        },
    }
