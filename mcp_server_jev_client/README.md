# Godot observation and voice-command service

`POST /observe` fetches a first-person frame through Godot's HTTP command API, summarizes
it with `gemini-3.5-flash`, and asks `jev-latest` for a decision through Backboard.
It **returns the decision without executing it**. The only game command it sends is
`observe`.

A Streamable HTTP MCP server is mounted at `http://127.0.0.1:8000/mcp` in the same
process. The `vod-voice` CLI submits spoken commands there. Those jobs are queued,
scored by Jev (including action probabilities), and—when the scores are clear
enough—dispatched to Godot through the same HTTP API. Godot no longer implements
MCP; do not point this service at `/mcp` on the game.

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
GODOT_API_URL=http://127.0.0.1:3000/api/v1/commands
```

`GODOT_MCP_URL` is obsolete. If only that variable is set, startup fails with a
migration error. `GODOT_API_URL` wins if both are present. Do not silently rewrite
an old `/mcp` URL.

Start a **rendered** Godot game bound locally, then run the Python service:

```sh
# from game/
VOD_API_BIND=127.0.0.1 godot-mono --path .

# from mcp_server_jev_client/
uv run --locked mcp-server-jev-client
```

The service listens on `127.0.0.1:8000` (`/observe` and `/docs`, plus MCP at `/mcp`).
It opens the Godot HTTP client during startup, so the game must already be running.
Missing API keys or a failed game URL configuration fail startup. Provider clients
and the game HTTP client are reused and closed on shutdown. Restart the service
after restarting the game.

Use one service instance and one worker: the game has one shared player. The
packaged command enforces one worker. `uv run --locked python interface.py` is an
equivalent compatibility entry point. The old `mcp_server.py` is an unrelated
stdio example, not this service.

Set `VOICE_DISPATCH=0` to keep the same queue and scoring contracts without
invoking the executor.

MCP clients call `get_command_context` before each new submission and pass its
`service_instance_id` and `generation` to `submit_voice_command`, alongside the
command ID and transcript. Every priority stop advances the generation and
cancels queued/scoring jobs, including when Jev selects `stop`. Delayed submissions
from an earlier generation or service instance are rejected with
`stale_command_context`; they are never automatically resubmitted. `vod-voice`
handles this protocol, including a stop while the context request is outstanding.

## Request and response

```sh
curl http://127.0.0.1:8000/observe \
  -H 'Content-Type: application/json' \
  -d '{"goal":"Find the red crystal"}'
```

The request requires a nonempty `goal`. No image upload is needed: the service
calls Godot's `observe` command to obtain the PNG and matching state.

The response contains:

| Field | Meaning |
| --- | --- |
| `observation_sequence` | Godot's capture sequence, scoped to that running game. |
| `game_state` | Original structured state, including position, camera, movement, held item, and future fields. |
| `observation` | Validated Gemini `summary`, `objects`, `possible_hazards`, and `uncertainties`. |
| `decision` | Jev's proposed `action` and normalized `arguments`. |
| `timings_ms` | Elapsed `mcp`, `gemini`, `jev`, and `total` processing times. |

`timings_ms.mcp` and error `stage: "mcp"` still refer to **game observation**, now
over HTTP. Those names are legacy labels kept for `/observe` compatibility.

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

Actions are `walk_forward`, `rotate`, `stop`, `grab_item`, `drop_item`, and `wait`.
Rotation uses relative yaw from −180° to +180° in 2° increments; positive turns
right. This sign is opposite Godot's world Y rotation. All other actions return
empty arguments. `wait` means no proposed tool call and is not a Godot command.

Jev receives the goal, original state, and validated visual summary as JSON. Its
typed System One answers are normalized into the response. Requests are
independent, with memory disabled and no reused conversation thread. Backboard
still creates provider-side message/thread records for each call. Tools are not
sent to Jev; Python application code invokes MCP and game operations.

The game continues running while models respond. A decision describes its captured
frame and may already be stale when returned. `/observe` does not execute
decisions or run an agent loop.

## Voice commands

Voice capture uses local `parakeet-mlx` (`mlx-community/parakeet-tdt-0.6b-v3`) on
Apple Silicon. The Hugging Face weights file is about **2.51 GB**, plus additional
runtime memory and cache. Raw microphone audio stays on the machine; only the
transcript is sent to Jev through Backboard.

Install the optional extra (include `--extra voice` on later `uv run` commands so
uv does not drop it):

```sh
uv sync --locked --extra voice
uv run --locked --extra voice vod-voice --list-devices
uv run --locked --extra voice vod-voice --transcribe-only
```

`--list-devices` exits without loading the model. `--transcribe-only` validates
STT without MCP, game, or provider keys.

Hold **F8** (rebind with `--hotkey`) while Godot is focused, speak one action, and
release. Default **F9** is an immediate priority stop that does not wait for
recognition or Jev. Exact utterances `stop`, `stop moving`, and `cancel all
actions` take that same control path. “Do not stop” does not.

F8 may require the Fn key or a macOS function-key setting; rebind if needed.
macOS microphone permission and Input Monitoring (and Accessibility, if pynput
asks) must be granted to the **actual launching app**—often Terminal, iTerm, or
the Python interpreter—then relaunch that app. Root is not required. The
microphone indicator stays on for the whole CLI lifetime because the input stream
is kept open; idle samples are discarded and not stored.

V1 executes one action per utterance: walk, rotate, stop, grab, drop, or wait.
Broad goals may produce one next action. Compound instructions such as “turn
right then pick it up” are reported as needing clarification instead of executing
only part. `walk forward` is a **bounded 1-second step** followed by `stop`. That
is not distance control. A process crash can defeat the Python timer; a
crash-safe walk lease would need a game-side change and is out of v1.

Recommended STT target, unverified on this Mac/game combination: warm p95
release-to-transcript ≤ 750 ms for 2–10 s utterances with Godot running. Gemini
and Jev time are separate and are not part of that STT claim.
The CLI reports both inference `STT` time and `Release-to-transcript` time; the
latter includes audio sealing/resampling and local scheduling after key release.

End-to-end, with the extra in both terminals so concurrent `uv` commands do not
reconcile the environment differently:

```sh
# Terminal 1, from mcp_server_jev_client/
uv run --locked --extra voice mcp-server-jev-client

# Terminal 2, from the same directory
uv run --locked --extra voice vod-voice
```

## Failures and logging

| HTTP status | Meaning |
| --- | --- |
| `409` | An observation is already running; requests are not queued. |
| `422` | Invalid request body or empty goal. |
| `502` | Game/provider failure, missing observation data, or invalid model output. |
| `504` | Upstream timeout, Godot request expiry, or the overall 60-second deadline. |

The game HTTP read timeout is 15 seconds, longer than Godot's 10-second deadline.
Errors have a `detail` object with `stage` (`admission`, `mcp`, `gemini`, or `jev`)
and an `error` code. A failure stops the pipeline and does not fabricate a result.
Headless Godot cannot capture screenshots and returns an observation error.
Timeouts release the request slot; cancelling an inference may not prevent the
provider from billing work already accepted.

Application logs contain capture sequence IDs, stage timings, and sanitized
failure stages. They exclude image payloads, prompts, and credentials. Raw images
are sent to Gemini and are not included in the endpoint's response.

## Verification

```sh
uv sync --locked
uv run --locked pytest -q
uv sync --locked --extra voice
uv run --locked --extra voice vod-voice --list-devices
uv run --locked --extra voice vod-voice --transcribe-only
```

The default suite uses mocked providers and includes the full pipeline, unchanged
image forwarding without size checks, supported decisions and rotation signs,
malformed responses, deadlines, cancellation, overlapping requests, scored Jev
distributions, the command queue, bounded execution, MCP tools, and assertions
that `/observe` never dispatches gameplay commands.

For a live `/observe` check, configure the API keys and start rendered Godot, then run:

```sh
RUN_LIVE_OBSERVE=1 uv run --locked pytest -q -m live
```

This opt-in test makes paid calls to Gemini and Backboard and observes the running
game once. It checks the response contract and records that only `observe` was
sent. It does not require the HTTP service to be running separately.

Runtime dependencies are declared in `pyproject.toml` and locked in `uv.lock`.
`requirements.txt` delegates to the package for pip compatibility; use uv for
reproducible installs.
