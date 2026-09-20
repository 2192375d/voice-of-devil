# Low-latency hinted observations

The voice goal loop uses **Godot-only** `get_game_state()` observations: fresh
structured state and object hints go directly to Jev with `vision: null`. It does
not initialize or call Gemini, even when background vision is configured. Godot
still captures a PNG for its existing observe endpoint; the voice path discards
that image. It replans after each completed action until Jev chooses `done` or
`wait`, or until its step/time safety limit is reached. The original goal and a
compact action history accompany each fresh state. See
[voice commands](README.md#voice-commands-godot-state--jev-goal-loop)
for action dispatch. The Gemini pipeline below remains available to explicit MCP
`observe` callers and benchmarks.

Recording and goal execution run concurrently. Beginning a new Enter-based
recording pauses future planning after `VOICE_STEERING_DELAY_MS` (500 ms by
default). Valid transcripts replace the previous goal through a latest-wins
mailbox; invalid recordings resume it. Compatible active movement is preserved,
same-axis movement is replaced through targeted Godot controls, and exact `stop`,
`halt`, or `cancel` transcripts clear pending work and stop both axes without Jev.
The exact-stop path sends prompt controls outside the normal dispatch lock, then
repeats an ordered clear-and-stop barrier after in-flight work settles. If that
barrier cannot be confirmed, autonomous dispatch stays blocked until reconciliation.
This assumes the voice supervisor is the only action-producing client for the
player; Godot queue clearing and active movement are global, not client-owned.

Godot supplies the unchanged 512×512 PNG, matching state, and approximate visible
object hints. Gemini receives the PNG plus compact hint JSON and perception
instructions. The voice goal is supplied separately to Jev; it is never passed
into this observation pipeline. No screenshot resizing, overlays, or detector
model is involved, and image dimensions are not validated.

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
    "source_observation_sequence": 40,
    "cache_hit": true,
    "source_age_ms": 760.0,
    "summary": "A door is visible on the left.",
    "objects": [{"label": "door", "screen_region": "left"}],
    "possible_hazards": [],
    "uncertainties": []
  },
  "timings_ms": {
    "capture": 18.0,
    "capture_queue": 0.0,
    "queue": 0.0,
    "gemini": 0.0,
    "vision_wait": 0.1,
    "total": 18.2
  }
}
```

The example abbreviates game state: all other existing gameplay/camera fields
are preserved. Transport `status`/`message`, preprocessing hints, sequence, and
simulation time are excluded from `game_state`. Jev receives the dictionary
serialized as JSON in its existing `game_status` field. External callers that
previously parsed the text headings must switch to these fields.

Gemini's existing object response fields are unchanged; hint IDs are not yet
returned by Gemini. The example reuses an exact matching visual summary from
sequence 40 alongside freshly captured state from sequence 42. The outer
sequence always identifies current state; `vision.source_observation_sequence`
and `source_age_ms` identify the summary's original source frame. On a miss,
observation waits for the requested frame's validated summary. It never returns
an unmatched old scene to meet a deadline. Errors and truncated JSON propagate
as errors rather than a fabricated description and are never cached.

## Request settings and background vision

The default `optimized` profile retains `gemini-3.5-flash`, explicitly uses minimal
thinking, and limits output to 1,024 tokens. The prompt asks for one concise
summary sentence, at most six salient objects, two hazards, and two uncertainties.
It notes that the player may hold an item in the bottom-right of the first-person
view and that authored hints can mislabel its rendered appearance. A truncated
response is rejected, even if its text happens to parse as JSON.

Four named profiles can be benchmarked through `GeminiVision(profile=...)`:

| Profile | Model | Thinking | Image media resolution |
| --- | --- | --- | --- |
| `baseline` | Gemini 3.5 Flash | Previous API default | Previous API default |
| `optimized` | Gemini 3.5 Flash | Minimal | API default |
| `low` | Gemini 3.5 Flash | Minimal | Low |
| `lite` | Gemini 3.5 Flash-Lite | Minimal | Low |

`baseline` retains the previous prompt/output settings for comparison. The other
profiles share the short prompt and 1,024-token ceiling. Low media resolution is
a Gemini input setting, not a resize or crop of the supplied PNG. It may lose
small-object detail, so neither it nor Flash-Lite is selected automatically.
The reusable Gemini client disables automatic retries (one attempt), keeping
failed requests visible in latency results instead of silently paying retry time.
The MCP vision entrypoint uses `GEMINI_VISION_PROFILE=optimized` by default. Set it
explicitly to another profile only after reviewing that candidate's measurements
and quality; no benchmark automatically updates this environment setting.

The MCP entrypoint manages a persistent vision service and its clients.
Background capture/inference is now **off by default** to protect API quota.
Opt in with `GEMINI_BACKGROUND_VISION=1`. When enabled, capture runs every 500 ms,
including while idle; this is not an API request rate. The service
permits one background inference, one replaceable pending
speculative frame, and one foreground inference. A foreground miss does not
queue behind an unrelated background call, but must pass the shared quota guard;
identical in-flight requests share one result. Background work yields to known
foreground jobs, retains spare quota for foreground use, and pauses on deferral.
Only the latest pending capture survives that pause. Background sampling can
incur paid requests while the game changes, even without an observation request.

Only successful summaries enter the bounded cache (16 entries, 30-second TTL
from source capture, never extended by cache hits).
Keys include exact PNG bytes, canonical hint JSON, and model/prompt/schema/settings,
but exclude the changing observation sequence. A detected game restart invalidates
the session's cache (a repeated/backward sequence or backward simulation time).
Every foreground observation still obtains current Godot
state and matches that exact frame; tiny camera, animation, or hint-box changes
can cause a miss. This deliberately does not use perceptual similarity as proof
that a stale description is correct. Shutdown cancels workers and closes clients.

`timings_ms.capture_queue` measures waiting for capture, `capture` the game request,
`queue` the inference-lane wait, and `vision_wait` the caller's actual wait after
capture. `gemini` is the originating inference time on a miss/shared in-flight
request, or zero for a completed cache hit. `total` measures complete observation
delivery; components need not sum when callers share background work. These are
vision timings, not Jev decision or action-acknowledgment latency.

The voice loop logs `decision_action_ms` for individual acknowledgments. It excludes
recording and speech transcription and does not establish that movement physically
completed. Each loop step executes one decision, allowing walking and turning
together through the explicit `walk_and_turn` choice. There
are no Gemini timings or Gemini quota gates in this path.

## Quota safeguards

Every `GeminiVision` request, including benchmark calls, goes through a persistent
SQLite admission guard. Safe local defaults apply without editing your `.env`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `GEMINI_RPM_BUDGET` | `4` | Requests per rolling 61 seconds, also spaced at least 16 seconds apart |
| `GEMINI_INPUT_TPM_BUDGET` | `16000` | Reserved input tokens per rolling 61 seconds |
| `GEMINI_RPD_BUDGET` | `20` | Requests per rolling 24 hours plus one second |
| `GEMINI_INPUT_TOKEN_RESERVATION` | `4096` | Input allowance for image, system prompt, and schema; hint UTF-8 bytes are added |
| `GEMINI_FOREGROUND_RESERVE` | `1` | Request/day slots and input-token allowance withheld from background work |
| `GEMINI_BACKGROUND_VISION` | `0` | Background work requires explicit opt-in (`1`) |

These are deliberately conservative **application budgets, not verified Google
account limits**. Set budgets below the model's active limits in AI Studio and
allow room for other clients. Token reservation is an estimate: larger images or
different prompts may require increasing the allowance. Actual reported input
usage increases the ledger when it exceeds the reservation; smaller responses
never refund it. No extra `countTokens` requests or image-size validation are used.

The ledger is `.state/gemini-quota.sqlite3` next to the Python modules (git-ignored).
It contains only model/scope, timestamps, token counts, and cooldown state—no API
keys, screenshots, hints, or model descriptions. MCP, benchmarks, and
multiple processes in the same checkout share it; restarting does not reset it.
The baseline, optimized, and low profiles share one Flash budget, not three.
Database errors fail closed instead of bypassing the guard.

For other checkouts/processes using the same Google project, configure the same
absolute `GEMINI_QUOTA_DB` path, `GEMINI_QUOTA_SCOPE` (default `default`), and budgets.
This local SQLite file is not a distributed quota service for multiple machines.
Do not delete it or change scopes to circumvent a cooldown. Other apps using the
project but not this guard remain outside its accounting.

Budget exhaustion fails fast with `QuotaDeferredError`, a reason, and a suggested
`retry_after_seconds` where known. No unmatched stale scene is returned and no
automatic foreground retry is made. Exact matching cached summaries still work.
The MCP tool returns an error. The Godot-only voice loop does not use this quota.
The default quota cannot support a new cloud summary every second: low latency
and request throughput are different goals.

On provider 429/503, the guard persists a shared circuit-breaker cooldown. It honors
Google's structured `RetryInfo` and HTTP `Retry-After`, with a safety margin and
exponential fallback. A reported daily/zero quota causes a conservative 24-hour
pause rather than repeated probes. In-flight success cannot clear a newer cooldown.
Failed or cancelled admitted calls remain charged to the local ledger, and SDK
automatic retries stay disabled. No client can promise zero 429s: provider capacity,
untracked project usage, other quota dimensions, and account limits can change.
See [Google's quota documentation](https://ai.google.dev/gemini-api/docs/rate-limits).

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

## Capture and annotation

Start a rendered game and capture representative scenes, including occlusion
and movement. From this directory:

```sh
python benchmark_observe.py capture /tmp/vod-frames --count 30 --interval 0.5
```

Capture sends only `observe` commands; files include screenshots and state.
The interval is a delay after each capture. Existing frame files are never
overwritten. Edit annotations in each saved file after inspecting its image. For
example, add these fields alongside the existing `capture_ms` and `frame`:

```json
{
  "scene_categories": ["held_item", "small_object", "wrong_label"],
  "expected_labels": ["green cylinder", "door"],
  "critical_labels": ["green cylinder"],
  "expected_absent_labels": ["red sphere"],
  "expected_hazards": [],
  "critical_hazards": [],
  "expected_absent_hazards": ["fire"]
}
```

The representative dataset must cover `stationary`, `movement`, `occlusion`,
`held_item`, `small_object`, and `wrong_label`. Several tags can apply to one
frame, but tag real scene conditions; do not mark one stationary image as a
movement test. Expected labels/hazards are exact, case-insensitive strings.
Absent lists identify common wrong descriptions. Unless supplied, critical lists
default to all expected labels/hazards; absent-list violations are always critical.
Use `[]` for a verified absence of hazards and `null` for unannotated scenes.
Do not derive annotations from authored hints or a model answer: verify pixels.

## Fresh-inference profile benchmark

Set `GEMINI_API_KEY` in the environment or this directory's `.env`; never commit
the key. This command schedules **up to 404 paid Gemini calls** by default: one first-client
request and 100 warm attempts for each of four profiles. All candidates see the
identical saved PNG/hints for every paired attempt; request order is counterbalanced.
Clients are reused and the summary cache is bypassed.
The quota guard is never bypassed: replay stops at the first quota deferral and
writes a partial report with `halted`, reason, and retry delay. Such a run cannot
qualify. The conservative default budgets are intentionally insufficient for an
unpaced 404-call run; configure verified capacity before a qualification benchmark.

```sh
python benchmark_observe.py profiles /tmp/vod-frames --output /tmp/vod-profiles.json
```

The report separates first-client ("cold") requests from warm complete-summary
p50/p95. "Cold" does not guarantee a cold remote model. It includes token usage,
failures/truncations, descriptions, recognition errors, and the proportion of
**all attempts** that returned a valid summary under one second. Failed/time-out
requests are excluded from successful latency percentiles but remain failures
and stay in that rate's denominator. Capture-plus-summary is a replay estimate.

Each candidate's automated quality gate requires at least 100 warm attempts,
fully annotated successful baseline pairs, all six scene categories, no new
critical errors, and no increase in total recognition errors. Unannotated or
smoke runs cannot qualify. This gate is a screening tool, not semantic proof:
manually review synonyms, unlisted hallucinations, and held-item/occlusion detail
before choosing a lower-resolution or cheaper model. The command never changes
production settings, and a quality pass does not mean latency passed.

For a smaller **12-call smoke check**, not a performance/quality qualification:

```sh
python benchmark_observe.py profiles /tmp/vod-frames --warm-calls 2 --smoke --output /tmp/vod-smoke.json
```

Use `--profiles baseline optimized` to narrow a subsequent comparison. Use
representative frames and repeated runs; no cloud-latency improvement is assumed.

## Actual vision-wait benchmark

With a rendered game running, measure the managed service, including background
work, capture, queue waits, exact-cache hits, and foreground misses:

```sh
python benchmark_observe.py observe-wait --background --scene-category movement --count 100 --output /tmp/vod-wait-movement.json
python benchmark_observe.py observe-wait --background --scene-category stationary --count 100 --output /tmp/vod-wait-stationary.json
```

Move/change the scene manually during the movement run. This command sends only
observation commands, not movement or other actions. It waits five seconds for
background warmup (`--warmup`) and pauses 500 ms between foreground observations
(`--interval`). Without `--background`, no prewarming is started. Local quota
deferrals remain failures, never fast successes; reports include their reasons
and retry delays. These commands make paid background and foreground Gemini calls;
their count depends on changed frames and cache reuse, not only `--count`.
Reports show overall and hit/miss p50/p95, actual queue/capture/vision waits,
cache-hit rate, source age/sequence, and failures. Stationary cache hits alone
cannot demonstrate subsecond changing-scene performance. This benchmark excludes
Jev decisions and action acknowledgments; measure that full cycle separately.

## Legacy paired hint benchmark

With `GEMINI_API_KEY` configured, this command makes **two paid Gemini calls
per frame** and writes a new report:

```sh
python benchmark_observe.py evaluate /tmp/vod-frames --output /tmp/vod-report.json
```

Both conditions use the default optimized profile and identical PNG bytes. Request order alternates between
frames. The report includes responses, failures, recognition errors, and p50/p95
Gemini and capture-plus-Gemini latency. Recognition scoring is exact,
case-insensitive label matching; review synonyms and unlisted hallucinations
manually. No annotations means recognition errors are reported as null.

Capture latency includes hint generation in **both** conditions, making this a
comparison of sending versus withholding hints, not a pre-change performance
baseline. It is a replay estimate, excludes Jev, and is not full action latency.
Inspect Godot's hint timings separately. Use representative frames and repeated
runs; no cloud-latency or accuracy improvement is assumed.
