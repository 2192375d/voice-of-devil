```sh
VOD_API_BIND=127.0.0.1 VOD_API_PORT=3000 godot-mono --path .
```

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `VOD_API_BIND` | `0.0.0.0` | Listen on all interfaces; use `127.0.0.1` for local-only access. |
| `VOD_API_PORT` | `3000` | HTTP port. |
| `VOD_API_ORIGINS` | empty | Comma-separated allowed Origin header values. Requests without Origin are accepted. |

These override exported properties on the autoload. Authentication and TLS remain
deferred. All callers share one player and inbox. Startup logs the endpoint;
invalid configuration or a busy port logs an error without stopping the game.
Old `VOD_MCP_*` settings and `/mcp` are no longer supported.

## Call commands
Send `POST http://<game-host>:3000/api/v1/commands` with
`Content-Type: application/json`. No initialization, MCP headers, Accept header,
or client-supplied request ID is required. Bodies are limited to 64 KiB.

```sh
curl http://127.0.0.1:3000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command":"walk_forward","arguments":{"meters":5}}'

curl http://127.0.0.1:3000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command":"rotate","arguments":{"degrees":{"x":0,"y":90,"z":0}}}'

curl http://127.0.0.1:3000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command":"stop"}'

curl http://127.0.0.1:3000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command":"cancel_rotation"}'
```

| Command | Arguments | Behavior |
| --- | --- | --- |
| `walk_forward` | `{"meters":5}` | Finite meters (negative moves backward, zero completes immediately); stops after `abs(meters) / speed` simulation seconds, even when blocked. Repeats return `already_running` without resetting the timer. |
| `rotate` | `{"degrees":{"x":0,"y":90,"z":0}}` | Add relative yaw at fixed speed; positive Y turns right. X/Z must be zero; Y must be finite. |
| `stop` | `{}` or omitted | Cancel all active actions; preserve pending requests and held item. |
| `cancel_walk` | `{}` or omitted | Idempotently cancel active walking while preserving rotation and pending requests. |
| `cancel_rotation` | `{}` or omitted | Idempotently cancel active rotation while preserving walking and pending requests. |
| `grab_item` | `{}` or omitted | Pick the nearest unobstructed item in the forward hemisphere; requires idle movement and empty hands. |
| `interact` | `{}` or omitted | Toggle the nearest unobstructed door in front within 5 units; requires idle movement. Returns `opening`, `closing`, `busy`, `no_interactable_in_reach`, or `plate_controlled` (door follows its pressure plate automatically). |
| `drop_item` | `{}` or omitted | Release the held item if space is clear; requires idle movement. |
| `observe` | `{}` or omitted | Return a 512×512 first-person PNG with matching state. |
| `clear_queue` | `{}` or omitted | Cancel earlier pending action requests across all clients, preserving active actions, observations, and later requests. There is no ownership isolation. |

For pickup, drop, observation, and clearing:

```sh
curl http://127.0.0.1:3000/api/v1/commands -H 'Content-Type: application/json' -d '{"command":"grab_item"}'
curl http://127.0.0.1:3000/api/v1/commands -H 'Content-Type: application/json' -d '{"command":"drop_item"}'
curl http://127.0.0.1:3000/api/v1/commands -H 'Content-Type: application/json' -d '{"command":"observe"}'
curl http://127.0.0.1:3000/api/v1/commands -H 'Content-Type: application/json' -d '{"command":"clear_queue"}'
```

## Response contract

Every response uses this JSON envelope:

```json
{
  "request_id": "server-generated-id",
  "ok": true,
  "result": {"status": "started", "message": null},
  "image": null
}
```

`request_id` identifies this HTTP call for diagnostics, not a cancellable action
or an idempotency key. `started` means accepted and started on the physics thread,
not completed. Walk and rotate may run together. Conflicting actions return
`busy` immediately instead of waiting for compatibility.

Valid command requests return HTTP 200; always inspect `ok` and `result.status`.
Success statuses: `started`, `already_running`, `stopped`, `picked_up`, `dropped`,
`observed`, `cleared`. Clearing also returns `result.cleared_count`.
Failure statuses: `busy`, `hands_full`, `hands_empty`, `no_item_in_reach`,
`drop_blocked`, `not_ready`, `expired`, `queue_full`, `cancelled`, `shutdown`,
`observation_error`, `execution_error`, `invalid_arguments`.

HTTP errors also use the envelope with `ok: false`, a `result.status`, and a
`result.message`: 400 invalid JSON/request/arguments, 403 disallowed Origin,
404 unknown route, 405 unsupported method, 408 body read timeout,
413 oversized body, 415 unsupported content type, 500 unexpected server error.

## Observation data

An observation returns `result.status: "observed"`, plus these fields in `result`:

| Field | Contents |
| --- | --- |
| `observation_sequence` | Increasing observation number for this game run. |
| `simulation_time` | Elapsed physics time in seconds. |
| `position`, `rotation_degrees`, `velocity` | World-space `{x,y,z}` vectors; meters, degrees, meters/second. |
| `grounded` | Whether the player is on the floor. |
| `held_item` | `null` or `{id, name}`; ID is valid only for the current instance. |
| `camera` | World `position`, `rotation_degrees`, `width`, `height`. |
| `active_instructions` | Array of instruction progress objects. |
| `pending_action_count` | Number of undispatched action requests. |
| `last_finished_instruction` | Latest finished instruction progress object, or `null`. |
| `hints` | Approximate Godot visual hints: `{source: "godot", bbox_format: "normalized_xyxy", objects: [{id, label, bbox}]}`; `null` if unavailable or invalidated before rendering. |

Instruction objects contain `type`, `status`, `elapsed_seconds`,
`requested_degrees`, `remaining_degrees`, and `result`; nonapplicable fields are
null. Rotation progress describes relative yaw. Snapshot world rotations use
Godot's native convention: positive-right command Y has the opposite sign from
Godot world yaw.

`image` is `{"mime_type":"image/png","data":"<base64 PNG>"}`. State is sampled
before rendering and PNG read after that same frame; gameplay continues throughout.
A headless instance returns `observation_error` because screenshots require rendering.

Hints cover visible pickables, doors, and pressure plates with approximate
occlusion filtering. They contain no puzzle connections or exact distances.
See [hinted observations](../../mcp_server_jev_client/OBSERVATIONS.md) for frame
alignment, limitations, Python output changes, and benchmark instructions.

Save and decode an observation (Python standard library only; this is a calling
example, not a Python MCP implementation):

```sh
curl http://127.0.0.1:3000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command":"observe"}' -o /tmp/observation.json
python3 -c 'import base64,json,pathlib; r=json.loads(pathlib.Path("/tmp/observation.json").read_text()); assert r["ok"], r; pathlib.Path("/tmp/observation.png").write_bytes(base64.b64decode(r["image"]["data"]))'
```

## Queue and adapter behavior

HTTP handlers validate and enqueue; only Godot's main thread invokes instructions.
There is one inbox in Godot, capacity 256, with up to 64 ordinary requests dispatched
per physics tick. `stop`, `cancel_walk`, `cancel_rotation`, and `clear_queue` run before
ordinary requests, in arrival order relative to each other. This lets a targeted
cancel replace an active movement axis before a new ordinary request starts on the
same tick. New arrivals during dispatch wait for the next tick.

The inbox and player are global. `clear_queue` can cancel older pending actions
submitted by any client, and stop/cancel controls affect shared active movement.
Use a single action-producing client unless a future protocol adds owner/action IDs;
additional observation-only clients are safe.

**A pending action can start after `stop`.** To discard pending work and stop active
actions, await `clear_queue`'s response, then call `stop`. Clearing cancels only
older pending walk/rotate/grab/drop/interact requests; their responses report `cancelled`.
The targeted cancellation commands have the same pending-request behavior as `stop`:
they affect only the selected active axis. Await `clear_queue` first when older pending
requests for that axis must not start afterward.

Undispatched requests expire after 10 seconds and cannot execute afterward.
Observations also time out after 10 seconds without a usable frame. Body reads
have a separate 10-second deadline. Shutdown resolves outstanding requests.
There is no individual request-cancellation endpoint. Disconnecting does not undo
an accepted action, and losing the Python connection does not cancel an accepted walk; its game-side timer still stops it.

Python adapters should forward requests concurrently so `observe` cannot
block `stop`, inspect both HTTP status and `ok`, and package observation image/state
for MCP. Use a response timeout longer than the game's 10-second deadline. Do not
automatically retry actions after an ambiguous connection failure: retrying a
relative rotation can turn twice. Reconnect and observe to decide the next action.

## Verification

```sh
dotnet build
DOTNET_ROLL_FORWARD=Major dotnet run --project tests/TargetedCancellation/TargetedCancellation.csproj
```

Roll-forward is needed only when .NET 8 is absent and a newer runtime is installed.
The targeted checks cover cancellation preservation/idempotency, strict command
parsing, response status, and control priority with a fake player. Rendered-game
checks are still needed for screenshots, actual movement, pickup, and
collision-dependent drop.
