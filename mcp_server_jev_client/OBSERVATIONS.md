# Hinted observations

Godot supplies the unchanged 512×512 PNG, matching state, and approximate visible
object hints. Gemini receives the PNG plus compact hint JSON and perception
instructions. The voice goal is supplied separately to Jev; it is never passed
into this observation pipeline. No screenshot resizing, overlays, or detector
model is involved.

## Contracts

Godot's existing HTTP response adds `result.hints`:

```json
{
  "source": "godot",
  "bbox_format": "normalized_xyxy",
  "objects": [
    {"id": "123", "label": "door", "bbox": [0.1, 0.2, 0.4, 0.9]}
  ]
}
```

Boxes are `[left, top, right, bottom]` normalized to the viewport. IDs last only
for the current game instance. There are no model confidence scores. A null or
missing hint object means Gemini uses the image alone; an empty objects list
does **not** establish that the scene is empty.

MCP `observe()` now returns a dictionary, replacing its previous formatted text:

```json
{
  "observation_sequence": 42,
  "simulation_time": 125.4,
  "game_state": {
    "position": {"x": 2.1, "y": 0.0, "z": 4.8},
    "held_item": null,
    "active_instructions": []
  },
  "vision": {
    "source": "gemini",
    "source_observation_sequence": 42,
    "summary": "A door is visible on the left.",
    "objects": [{"label": "door", "screen_region": "left"}],
    "possible_hazards": [],
    "uncertainties": []
  }
}
```

The example abbreviates game state: all other existing gameplay/camera fields
are preserved. Transport `status`/`message`, preprocessing hints, sequence, and
simulation time are excluded from `game_state`. Jev receives the dictionary
serialized as JSON in its existing `game_status` field. External callers that
previously parsed the text headings must switch to these fields.

Gemini's existing object response schema is unchanged; hint IDs are not yet
returned by Gemini. Observations still wait for Gemini and are not cached.
Gemini failures propagate as errors rather than a fabricated description.

## Visibility and frame matching

Hints cover unheld pickables, doors, and pressure plates. Visible mesh bounds
are projected and clipped to the near/far planes and viewport. Nine physics rays
per candidate approximate occlusion; a ray must hit that object's collider
first. The player is excluded. Up to 32 candidates are retained, ordered by
decreasing screen area with instance-ID tie breaking.

Physics preparation runs after ordinary gameplay updates. The prepared AI
camera is independent of subsequent player transforms and remains fixed until
rendering finishes. Preparation refreshes on intervening physics ticks. State
is finalized before rendering; the PNG is read after that same draw. Changes to
tracked transforms, visibility, collision/render layers, camera parameters, or
scene node count invalidate hints before capture. Hint failures fall back to
the original image. A headless game still cannot provide a screenshot.

This is approximate visibility, not pixel segmentation: thin/partly hidden
objects can be missed, and colliders can differ from visible geometry. Animated
mesh deformation and material transparency are not modeled. No hidden puzzle
connections, door-control relationships, or object distances are sent as hints.
Authored item names may be inaccurate (the current “Red sphere” asset has a
green cylindrical mesh); Gemini is instructed to correct labels from pixels.

Godot logs sequence and accumulated hint-preparation milliseconds. Python logs
game-request, Gemini, and total observation time, without image/prompt payloads.
Root entrypoints enable INFO logging to stderr. Logs can be enabled similarly
when embedding the modules.

## Verification

With the declared Python dependencies and pytest installed, run from repo root:

```sh
python -m pytest mcp_server_jev_client/tests -q
dotnet build game/words-of-devil.csproj
godot-mono --headless --path game res://tests/Observations/check.tscn --quit-after 240
VOD_API_BIND=127.0.0.1 godot-mono --rendering-method gl_compatibility --path game res://tests/Observations/check.tscn --quit-after 240
```

The C# fixture is compiled only in Debug. Headless checks cover geometry and
physics visibility; rendered checks additionally verify PNG capture, camera
pose refresh, sequences, lock release, and stale-hint rejection. Both print
`PASS` on completion; a timeout without `PASS` is not a successful test.

## Paired benchmark

Start a rendered game and capture representative scenes, including occlusion
and movement. From this directory:

```sh
python benchmark_observe.py capture /tmp/vod-frames --count 30 --interval 0.5
```

Capture sends only `observe` commands; files include screenshots and state.
The interval is a delay after each capture. Existing frame files are never
overwritten. Edit `expected_labels` in each saved file to list manually verified
object labels, and optionally `expected_absent_labels` to identify common wrong
labels. Leave `expected_labels` null to skip automated recognition scoring.

With `GEMINI_API_KEY` configured, this next command makes **two paid Gemini calls
per frame** and writes a new report:

```sh
python benchmark_observe.py evaluate /tmp/vod-frames --output /tmp/vod-report.json
```

Both conditions use identical PNG bytes. Request order alternates between
frames. The report includes responses, failures, recognition errors, and p50/p95
Gemini and capture-plus-Gemini latency. Recognition scoring is exact,
case-insensitive label matching; review synonyms and unlisted hallucinations
manually. No annotations means recognition errors are reported as null.

Capture latency includes hint generation in **both** conditions, making this a
comparison of sending versus withholding hints, not a pre-change performance
baseline. It is a replay estimate, excludes Jev, and is not full action latency.
Inspect Godot's hint timings separately. Use representative frames and repeated
runs; no cloud-latency or accuracy improvement is assumed.
