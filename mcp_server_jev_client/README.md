# Godot observation service

`POST /observe` fetches a first-person frame through Godot's MCP server, summarizes
it with `gemini-3.5-flash`, and asks `jev-latest` for a decision through Backboard.
It **returns the decision without executing it**. The only MCP tool it calls is
`observe`; it does not stop existing movement or otherwise control the player.

Godot supplies a 512×512 PNG from the player's eyes. The service forwards the
original PNG bytes without checking dimensions, resizing, cropping, or decoding
the image. Image presence and base64 transport encoding are checked. Model JSON
and the required game-state fields are validated.

## Run locally

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). From this directory:

```sh
uv sync --locked
cp .env.example .env
```

Fill in `.env` (it is ignored by git):

```dotenv
GEMINI_API_KEY=your-google-api-key
BACKBOARD_APIKEY=your-backboard-api-key
GODOT_MCP_URL=http://127.0.0.1:3000/mcp
```

Start a **rendered** Godot game with its existing MCP autoload enabled, then run:

```sh
uv run --locked mcp-server-jev-client
```

The service listens on `127.0.0.1:8000`, with API documentation at `/docs`. It
initializes its MCP connection during startup, so Godot must already be running.
Missing API keys or a failed MCP initialization fail startup. Provider clients
and the MCP connection are reused and closed on shutdown. Restart the service
after restarting the game or losing the MCP connection.

Use one service instance and one worker: the game has one shared player. The
packaged command enforces one worker. `uv run --locked python interface.py` is an
equivalent compatibility entry point. The old `mcp_server.py` is an unrelated
stdio example, not the observation service.

## Request and response

```sh
curl http://127.0.0.1:8000/observe \
  -H 'Content-Type: application/json' \
  -d '{"goal":"Find the red crystal"}'
```

The request requires a nonempty `goal`. No image upload is needed: the service
calls Godot's existing `observe` tool to obtain the PNG and matching state.

The response contains:

| Field | Meaning |
| --- | --- |
| `observation_sequence` | Godot's capture sequence, scoped to that running game. |
| `game_state` | Original structured state, including position, camera, movement, held item, and future fields. |
| `observation` | Validated Gemini `summary`, `objects`, `possible_hazards`, and `uncertainties`. |
| `decision` | Jev's proposed `action` and normalized `arguments`. |
| `timings_ms` | Elapsed `mcp`, `gemini`, `jev`, and `total` processing times. |

Each object contains a `label` and `screen_region` (`left`, `center`, or `right`).
Directions are relative to the first-person view. Gemini describes visible
evidence and uncertainty; authoritative coordinates come from Godot. The summary
does not establish exact distances or guaranteed traversability.

An example decision is:

```json
{
  "action": "rotate",
  "arguments": {"degrees": {"x": 0, "y": 90, "z": 0}}
}
```

Actions are `walk_forward`, `rotate`, `stop`, `grab_item`, `drop_item`, `interact`, and `wait`.
`interact` toggles a nearby unobstructed door in front and requires idle movement.
Rotation uses relative yaw from −180° to +180° in 2° increments; positive turns
right. This sign is opposite Godot's world Y rotation. All other actions return
empty arguments. `wait` means no proposed tool call and is not a Godot MCP tool.

Jev receives the goal, original state, and validated visual summary as JSON. Its
typed System One answers are normalized into the response. Requests are
independent, with memory disabled and no reused conversation thread. Backboard
still creates provider-side message/thread records for each call.

The game continues running while models respond. A decision describes its captured
frame and may already be stale when returned. A future executor must account for
this, explicitly stop continuous walking, and handle the game's action rules.
This endpoint does not automatically execute decisions or run an agent loop.

## Failures and logging

| HTTP status | Meaning |
| --- | --- |
| `409` | An observation is already running; requests are not queued. |
| `422` | Invalid request body or empty goal. |
| `502` | MCP/provider failure, missing observation data, or invalid model output. |
| `504` | Upstream timeout or the overall 60-second deadline. |

Errors have a `detail` object with `stage` (`admission`, `mcp`, `gemini`, or `jev`)
and an `error` code. A failure stops the pipeline and does not fabricate a result.
Headless Godot cannot capture screenshots and returns an MCP observation error.
Timeouts release the request slot; cancelling an inference may not prevent the
provider from billing work already accepted.

Application logs contain capture sequence IDs, stage timings, and sanitized
failure stages. They exclude image payloads, prompts, and credentials. Raw images
are sent to Gemini and are not included in the endpoint's response.

## Verification

```sh
uv run --locked pytest -q
```

The default suite uses mocked providers and includes the full pipeline, unchanged
image forwarding without size checks, supported decisions and rotation signs,
malformed responses, deadlines, cancellation, overlapping requests, and assertions
that no gameplay action tool is called.

For a live check, configure the API keys and start rendered Godot, then run:

```sh
RUN_LIVE_OBSERVE=1 uv run --locked pytest -q -m live
```

This opt-in test makes paid calls to Gemini and Backboard and observes the running
game once. It checks the response contract and records that only `observe` was
called. It does not require the HTTP service to be running separately.

Runtime dependencies are declared in `pyproject.toml` and locked in `uv.lock`.
`requirements.txt` delegates to the package for pip compatibility; use uv for
reproducible installs.
