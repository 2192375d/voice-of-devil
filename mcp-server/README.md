# Game MCP server

The `McpServer` autoload starts with the game. Connect an MCP Streamable HTTP client
to `http://<game-host>:3000/mcp`. The server controls one shared player. The Jev
client is developed separately; no Jev SDK or API key is needed by the game.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `VOD_MCP_BIND` | `0.0.0.0` | Listen on all interfaces; set `127.0.0.1` for local connections. |
| `VOD_MCP_PORT` | `3000` | HTTP port. |
| `VOD_MCP_ORIGINS` | empty | Comma-separated allowed Origin header values. Requests without Origin are accepted; supplied origins must match. |

The matching exported properties on `McpServer` provide defaults when environment
variables are absent. Authentication and TLS are deferred. There is no session
isolation: clients share the player and request inbox. Use unique request IDs
across simultaneously connected clients; duplicate in-flight IDs are rejected.

The game logs the endpoint at startup. Invalid configuration or a busy port logs
an error without stopping the game. Run a rendered Godot instance for screenshots;
headless instances return `observation_error`.

## Tools

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `walk_forward` | `{}` | Continuous walking; repeats return `already_running`. |
| `rotate` | `{"degrees":{"x":0,"y":90,"z":0}}` | Relative yaw at fixed speed. Positive Y turns right; X/Z must be zero. |
| `stop` | `{}` | Cancel active walking/rotation. Preserve pending requests and held items. |
| `grab_item` | `{}` | Grab the nearest unobstructed item in reach; movement must be idle. |
| `drop_item` | `{}` | Release the held item when clear; movement must be idle. |
| `observe` | `{}` | Return a 512×512 first-person PNG and matching state. |
| `clear_queue` | `{}` | Cancel earlier pending walk/rotate/grab/drop requests; active actions continue. |

Action responses report the instruction manager's result immediately after main-thread
dispatch. `started` means the action was started, not that movement has finished.
Success results include `started`, `already_running`, `stopped`, `picked_up`, and
`dropped`. Failures include `busy`, `hands_full`, `hands_empty`, `no_item_in_reach`,
`drop_blocked`, `not_ready`, `expired`, `queue_full`, `cancelled`, and `shutdown`.
Gameplay failures set `isError: true`. Malformed tool names/arguments produce
JSON-RPC errors. Every tool result includes JSON text content and the same object
in `structuredContent`; observations add an MCP image content block.

Observation state includes simulation time, an observation sequence, position,
Godot world rotation in degrees, velocity, grounded status, held-item name and
scene-instance ID, camera pose/dimensions, active instruction progress, pending
action count, and the latest finished instruction. Godot world yaw uses its native
sign convention; the rotate tool's positive-right convention is opposite that Y
sign. IDs last only for the current instance. State is sampled before rendering;
the PNG is read after that same frame. Gameplay continues during observation.

## Inbox behavior

Only the main physics thread invokes game instructions. HTTP threads validate and
enqueue requests. Capacity is 256 outstanding tool calls, with up to 64 ordinary
requests dispatched per physics tick. There is no gameplay compatibility queue:
conflicting actions return `busy` and are not retried.

`stop` and `clear_queue` take priority over ordinary requests, in arrival order
relative to each other. Clearing uses its arrival sequence as a cutoff, preserves
observations and later requests, and returns `cleared_count`. Discarded requests
receive a `cancelled` tool result. **A pending action can start after `stop`.**
To empty the inbox and stop the player, await `clear_queue`, then call `stop`.

Unprocessed requests expire after 10 seconds and cannot execute afterward.
Observations also time out after 10 seconds if no usable frame arrives. MCP
`notifications/cancelled` cancels pending calls or observations; it does not undo
already-dispatched gameplay actions. A dropped HTTP connection does not cancel an
accepted action. Shutdown resolves outstanding calls and closes the listener.

## Wire protocol and examples

Protocol revision: `2025-11-25`. Initialize first, send
`notifications/initialized`, then call `tools/list` and `tools/call`. Initialization
negotiates to the supported revision. Subsequent requests require
`MCP-Protocol-Version: 2025-11-25`. This stateless endpoint issues no session IDs.
GET and DELETE return 405; no SSE stream or legacy HTTP+SSE endpoint is provided.
POST requests require `Content-Type: application/json` and
`Accept: application/json, text/event-stream`. Bodies are limited to 64 KiB.

Initialization request:

```sh
curl http://127.0.0.1:3000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"manual-test","version":"1"}}}'
```

After sending the initialized notification with the negotiated version header,
an observation request is:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"observe","arguments":{}}}
```

The implementation follows the MCP [HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
and [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) specifications.

## Verification

```sh
dotnet build
DOTNET_ROLL_FORWARD=Major dotnet run --project tests/Instructions/Instructions.csproj
DOTNET_ROLL_FORWARD=Major dotnet run --project tests/Mcp/Mcp.csproj
```

`DOTNET_ROLL_FORWARD=Major` is only necessary when .NET 8 is absent and a newer
runtime is installed. The MCP checks exercise the real HTTP transport, protocol,
dispatcher, and instruction manager using a fake player, including concurrency,
priorities, expiry, cancellation, readiness, origin checks, and shutdown.

For standard-client interoperability, install `mcp==2.2.0` into a separate Python
environment and run:

```sh
python tests/Mcp/smoke_client.py --fixture
python tests/Mcp/smoke_client.py --url http://127.0.0.1:3000/mcp
```

The fixture uses the built C# test assembly and has no renderer. The real-game
check moves/turns/stops the player and expects a rendered screenshot. Also verify
in Godot that camera images match orientation and held items, and that walls block
pickup/drop. Automated fixture checks do not verify those engine behaviors.

Validation performed for this implementation: clean C# build, existing instruction
checks, 44 MCP checks, and official Python SDK interoperability against both the
fixture and a rendered Godot 4.7.2 instance. Live checks confirmed eye height,
512×512 PNGs, movement/rotation, pickup, held-item observations, release, and a
wall-blocked release that preserved ownership until the player turned away.
