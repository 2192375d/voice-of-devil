Implementation handoff: voice commands, Jev action probabilities, and queued execution

This is a proposed implementation specification, reviewed against branch `voice`, commit `f8ad52b`, on 2026-09-19 and revised for the user's clarification that voice should drive queued game actions scored by Jev through Backboard. It supersedes the earlier transcription-to-observation-only proposal. No feature code or hardware benchmarks were produced during this review. Recheck the working tree before implementation and preserve other changes. Work on `voice`; do not commit or push unless asked.

**1. Outcome and fixed scope**

On an Apple Silicon Mac, a developer starts the rendered game, Python service, and voice CLI. With Godot focused, they hold a configurable key, say “turn right ninety degrees,” and release. The CLI promptly displays the transcript and submits one command job through the Python MCP server. The job enters a bounded queue; Jev evaluates the transcript and game context through Backboard and returns action probabilities. Application code validates the decision and dispatches an eligible action to Godot, then reports the scores and game acknowledgement.

The working product assumption is automatic dispatch of sufficiently clear, valid decisions, because the user says voice should perform game actions. If they choose score-only operation, retain the same queue and scoring contracts and omit executor invocation. Do not make `/observe` execute actions: its existing read-only contract remains intact.

Keep local `parakeet-mlx` with `mlx-community/parakeet-tdt-0.6b-v3`. Build the command queue, scored-decision contract, narrow MCP surface, and bounded executor. Do not build paid/cloud STT, LLM cleanup, live captions, an overlay, clipboard injection, or an open-ended autonomous game loop. The existing Gemini/Backboard pipeline still has its existing credentials and costs. Raw microphone audio stays local; transcript text is subsequently sent to Jev through Backboard.

V1 handles one action per utterance: walk forward, rotate, stop, grab, drop, or wait. Broad goals such as “find the red crystal” can produce one next action, but do not imply automatic navigation to completion. Compound instructions such as “turn right then pick it up” must be reported as unsupported/needs-clarification rather than silently executing only part. Multi-action planning is a later feature.

**2. Architectural decisions and alternatives**

| Decision | Chosen implementation | Reason |
| --- | --- | --- |
| Process ownership | A separate `vod-voice` CLI inside the existing Python package | Keeps model loading, mic permissions, and global keyboard hooks out of the FastAPI process; existing typed clients remain lightweight. |
| Integration boundary | Voice CLI calls a Python MCP `submit_voice_command` tool; tools and HTTP routes share ordinary service functions | Explicitly supports the requested MCP connection while keeping queue and scoring logic transport-independent. |
| Recognition strategy | Warm, complete-utterance inference after release first; streaming only if measured latency misses the target | Short commands need a prompt final answer, not partial captions. This avoids streaming cache/tail complexity and repeated GPU work during speech. Performance must be measured. |
| Game transport | Python MCP service calls Godot's existing HTTP API for observation and validated execution | Godot no longer speaks MCP. MCP belongs on the Python-facing boundary; do not restore it in the game. |
| Queue ownership | One Python FIFO of command jobs, one worker, one executor | Queue transcripts first; classify against current state when each reaches the head. Never put unclassified text directly into Godot's command inbox. |
| Model role | Backboard SDK calls Jev for typed scores; Python dispatches tools | Jev's System One path returns structured answers, not model-directed MCP tool calls. |
| Hotkey | Python global key listener, configurable single key, default F8 | Works while Godot owns focus; keeps v1 in Python. A terminal-only key reader would fail this UX. |
| Dependencies | Optional `voice` extra with platform markers and lazy imports | Installing or starting the observation service must not require MLX, a microphone, a desktop session, or model downloads. |

A Godot hotkey helper would avoid global keyboard permissions but introduce a game-to-Python control protocol, focus/lifecycle coupling, and game changes. Revisit it for an integrated game UI, not this terminal-based v1. A separate process isolates Python lifecycles; it does not isolate the shared Apple GPU or memory.

**3. Correct the assumptions before coding**

The named model currently contains an approximately **2.51 GB** weights file. Document that download plus additional runtime memory and cache space; do not repeat the proposed 0.75–1.2 GB estimate. [Hugging Face model files](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3/tree/main).

Treat “70–100× realtime” and “0.3 seconds behind speech” as unverified on this game/Mac combination. Loading the model once is necessary, but warm-up and release-to-display measurements are also required.

Upstream exposes in-memory inference through `get_logmel(...)` and `model.generate(...)[0]`; `model.transcribe(...)` takes a file path. Streaming `result` includes draft tokens, and context exit performs cleanup rather than an explicit final flush. Do not assume `result.text` immediately at key-up has consumed queued audio or the final incomplete frame. Recheck the selected locked release. [Parakeet implementation](https://github.com/senstella/parakeet-mlx/blob/master/parakeet_mlx/parakeet.py).

The file-loading path uses FFmpeg. Use in-memory PCM for microphone inference to avoid a WAV write/read cycle and an unnecessary FFmpeg runtime requirement. [Audio implementation](https://github.com/senstella/parakeet-mlx/blob/master/parakeet_mlx/audio.py).

**4. First milestone: shared game transport and the real MCP service**

Change `src/mcp_server_jev_client/providers.py` and `app.py`:

- Retain `GodotObserver.observe() -> GameFrame` as an observe-only adapter over a reused HTTP game client. Add a separate narrow executor for gameplay commands; `/observe` receives only the observer capability.
- Add `GODOT_API_URL`, default `http://127.0.0.1:3000/api/v1/commands`. Replace the old game-facing MCP session initialization in `open_services()` with the HTTP client's lifecycle. Do not add a startup observation or restore MCP inside Godot; the new Python-facing MCP surface is specified below.
- Update `.env.example` and the README. If only the obsolete `GODOT_MCP_URL` is configured, give a targeted migration error instead of contacting `/mcp` or silently translating arbitrary URLs. The new variable wins if both are present.
- Inspect both HTTP status and the response envelope. Require `ok is True`, `result.status == "observed"`, valid `GameState`, and nonempty valid base64 `image.data` with `image.mime_type == "image/png"`. Preserve unknown state fields and original decoded image bytes. Do not introduce image resizing or dimension validation.
- Use a 15-second game read timeout, exceeding the game's 10-second deadline. Preserve the pipeline's existing overall 60-second timeout. Map game expiry and transport timeouts to the existing sanitized 504 error shape; other failed/malformed game responses use 502. Never call model providers after a game failure.
- Preserve the `/observe` request, response, 409 behavior, and provider semantics. For strict v1 compatibility, retain the legacy `timings_ms.mcp` and error `stage: "mcp"` labels for game observation and document their legacy meaning. Rename these only in a separately approved contract change.
- Update the existing mock, SDK-integration, and opt-in live observation tests from MCP calls/envelopes to HTTP requests/envelopes. Keep real Gemini/Backboard SDK serialization coverage. Assert the sole game command in the `/observe` path is `observe`.

Read the game's actual contract in `game/game-api/README.md`. Keep Godot's existing HTTP envelope and command names. Do not reuse the unrelated top-level `mcp_server.py` toy. No game changes are expected for the first bounded executor; hard crash-safe walking limits would require a separate game-side lease/watchdog enhancement.

Mount a real Streamable HTTP MCP server at `http://127.0.0.1:8000/mcp` alongside the existing FastAPI `/observe`. Use the locked MCP SDK's supported ASGI/session-lifecycle integration and test initialization plus `tools/list` and `tools/call`; a JSON route named `/mcp` is not an MCP server. One process/worker owns clients, queue, and lifecycle. The CLI uses a real MCP client session. Godot remains at `http://127.0.0.1:3000/api/v1/commands`; these are two different connections.

Expose only these application tools, each calling the same underlying job service:

| Tool | Contract |
| --- | --- |
| `get_command_context` | Read the current `service_instance_id` and stop `generation` before preparing a new submission. |
| `submit_voice_command` | Input `{command_id: UUID, transcript: nonempty string, service_instance_id: UUID, generation: int}`; reject stale instance/generation values and otherwise return an admission receipt and job status. |
| `get_voice_command` | Fetch job status, selected action/arguments, probabilities, timings, and eventual execution result by ID. |
| `cancel_voice_command` | Cancel a queued/scoring job; discard late scoring results. An already dispatched action cannot be undone by cancelling its job. |
| `stop_game` | Priority control: invalidate pending work, clear pending game actions, and stop active movement. Available while inference is running. |

Initialize once; close sessions/clients on shutdown. Refactor the current observation pipeline into reusable functions rather than making the queue worker issue loopback HTTP calls to `/observe`. Preserve the existing one-at-a-time observation admission behavior for external `/observe` callers. The worker waits for that same pipeline slot without losing its job; stop/control work must not wait for a model call to finish.

**4a. Preserve Jev's probabilities instead of discarding them**

The current `normalize_decision()` only keeps the selected action and yaw. Add a scored-result path while preserving `decide()`/`ObserveResponse` compatibility for read-only callers. Use Backboard `send_message`, `llm_provider="typesafe"`, `model_name="jev-latest"`, `memory="off"`, `stream=False`, with no reused thread. Keep the repository's existing `BACKBOARD_APIKEY` setting unless deliberately migrating it.

Backboard documents `choice`, `probabilities`, and `confidence` as separate response fields. Read `result.system_one.answers`; the selected action's probability is `probabilities[choice]`. `confidence` summarizes distribution concentration and is not that selected probability. The Jev route does not support tools: the Python application, not Backboard/Jev, invokes MCP/game operations. [Backboard System One guide](https://docs.backboard.io/concepts/system-one).

Create a typed `ScoredDecision` containing `action`, validated `arguments`, `action_probabilities`, `selected_action_probability`, `distribution_confidence` when supplied, `resolved_model`, and rotation-specific score information when applicable. These are model scores for the supplied decision question, not measured probabilities that physics execution will succeed. Do not fabricate missing scores or replace them with STT confidence.

Use the existing six action criteria plus an explicit `abstain` outcome for unsupported, compound, contradictory, or unclear requests. Revise instructions to prioritize the user's explicit command: a failed pickup precondition should produce a blocked result, not silently turn “pick up” into a different action. Broad goals may still choose a single next action. Keep Gemini's existing visual summary initially; bypassing vision for simple explicit commands is a later latency optimization, not required to deliver this feature.

Validate allowed keys, finite numeric probabilities in [0,1], a nonzero distribution, and consistency between choice and the highest-scoring option (allow ties). Preserve returned scores; use real SDK fixtures to define rounding tolerance rather than silently normalizing arbitrary invalid distributions. Reject missing/inconsistent action scores. For rotation, retain the bounded yaw choices and their probabilities; validate selected yaw and preserve its selected probability separately. Never multiply action and yaw scores and claim they form a calibrated joint probability.

Initial configurable execution policy: dispatch only if selected-action probability >= 0.80 and its lead over the next choice >= 0.15; rotation additionally requires valid arguments and a selected-yaw probability >= 0.80. These are conservative starting product thresholds to tune with command fixtures, not vendor guarantees. `abstain`, ties below the margin, and low-score results become `needs_clarification` without dispatch. Return all scores even when declining to act. `wait` completes locally with no game request.

**4b. Queue semantics and execution boundaries**

Use a bounded in-memory FIFO: default 16 pending jobs, one scoring/execution worker, and a 30-second queue-wait deadline. Expire old jobs before scoring them. Score at dequeue against a fresh observation, not at admission against a frame that may be stale by execution time. A full queue returns an explicit admission error. Preserve order; do not rank separate user utterances by their model probabilities.

Each job contains its client-generated ID, transcript, timestamps, status, cancellation generation, decision/scores, observation sequence, and game acknowledgement. Suggested lifecycle:

`queued -> scoring -> ready_to_execute -> dispatching -> dispatched -> completed`

Terminal alternatives: `needs_clarification`, `blocked`, `expired`, `cancelled`, `failed`, or `execution_unknown`. Distinguish admission, dispatch acknowledgement, and physical completion. Keep a bounded recent-result cache (e.g. 100 jobs for 10 minutes). Reusing an ID with the same payload returns the existing receipt/result; a different payload is an error. This deduplication is process-local, not durable exactly-once execution across restarts. Return a service-instance ID so a client can recognize lost history after a restart; never replay old jobs automatically.

After scoring, enforce freshness and preconditions before sending an action. Start with a configurable five-second maximum age of the decision's source frame; expire stale decisions rather than looping indefinitely through re-evaluation. For a non-stale decision, re-observe immediately before dispatch to check current movement, held item, and whether the relevant pose/scene has changed. A changed precondition produces `blocked`; it is not permission to execute a different action. This check narrows but does not eliminate the race with live gameplay; Godot's own validation is authoritative.

The executor allowlists supported commands and validates arguments using the existing models. Inspect HTTP status, envelope `ok`, and `result.status`. `started` means started, not completed. A timeout/disconnect after submission yields `execution_unknown`; do not retry relative rotations or pickup/drop automatically. Halt subsequent execution until fresh state has been reconciled; cancellation alone does not prove an action stopped.

Use these initial action semantics:

- `rotate`: relative yaw with the existing sign convention; serialize actions and poll observation until the rotation is no longer active, with a bounded completion deadline. Do not dispatch the next conflicting action upon receiving `started`.
- `grab_item` / `drop_item`: require idle movement and appropriate held-item state. Return the actual game outcome, including no item in reach or blocked drop. Do not automatically walk or stop to make the command succeed.
- `walk_forward`: the game walks indefinitely, so implement a documented bounded forward step (default one second), then explicitly issue `stop` and verify idle before completing the job. Clearly report this interpretation in CLI output; do not imply distance control or support arbitrary spoken durations. A process crash can defeat a Python timer; a hard crash-safe guarantee needs a game-side lease and is outside this v1.
- `stop`: use the priority stop operation, not ordinary FIFO scheduling.
- `wait`: report no action, with scores; do not call a nonexistent game `wait` command.

Provide a deterministic emergency control key (default F9) that invokes `stop_game`, independent of STT or Jev. Reserve exact normalized utterances `stop`, `stop moving`, and `cancel all actions` for the same immediate control path. Match the entire utterance, never a substring such as the word “stop” inside “do not stop.” Label these results `source: explicit_control` with probability fields absent rather than fabricated. Other utterances go through Jev scoring.

Priority stop first advances a cancellation generation and clears pending Python jobs, preventing late Jev responses from dispatching. Both Jev-selected stops and explicit controls use this queue-cancelling path. Admission validates each submission's service instance and generation under the queue lock so delayed requests cannot revive work after a stop. The CLI also cancels a submission if F9 occurs while fetching its context. Serialize game action sends against this stop barrier, account for any already-in-flight submission, then await Godot `clear_queue` followed by `stop`. Godot `stop` alone leaves pending actions intact. Do not hold this dispatch barrier across inference or physical action completion. If transport uncertainty prevents a confirmed clear/stop, report it and keep execution paused; never claim the game is stopped solely because local tasks were cancelled. Keep control traffic independent of observation/model admission locks.

On orderly shutdown cancel queued work and, if the executor owns active movement, attempt clear/stop before closing the game client. Do not resume or replay work after restart. For v1, this service is the sole action-producing client; other clients can still observe. If other controllers mutate the same player, exact completion attribution is not available in the current game API and needs a separate ownership/action-ID design.

**5. Voice CLI contract and lifecycle**

Register `vod-voice = "mcp_server_jev_client.voice:main"`. Use standard `argparse`; avoid a new CLI framework. Provide:

| Option | Default / purpose |
| --- | --- |
| `--mcp-url` | `http://127.0.0.1:8000/mcp` |
| `--hotkey` | `f8`; accept a supported single named key or character |
| `--stop-key` | `f9`; immediate priority stop independent of recognition/model latency |
| `--device` | System default input; accept device index/name |
| `--list-devices` | Enumerate input devices and exit without loading the model or contacting providers |
| `--transcribe-only` | Capture/display text with no MCP/game/provider calls; permits STT validation without API keys |

Use `pynput.keyboard.Listener` press/release callbacks. Maintain explicit key-down state to ignore auto-repeat and duplicate releases. Do not capture or log unrelated key contents or suppress the keyboard globally. F8 may require Fn or a macOS function-key setting; document rebinding. Keyboard callbacks should only hand off events. [pynput keyboard documentation](https://pynput.readthedocs.io/en/latest/keyboard.html).

Startup validates macOS/arm64, optional dependencies, device support, and listener trust; loads the model once on its inference worker; performs one warm-up inference; then prints `Ready — hold F8 to talk`. Distinguish downloading/loading/warming from ready. Load/warm failures are actionable errors, not a background listener that silently cannot transcribe. Use the normal Hugging Face cache and support cached offline STT; never download weights on import or in tests.

macOS microphone and keyboard monitoring permissions are separate. Document granting permissions to the actual launching app/interpreter, any required relaunch, and Accessibility if the listener backend requests it. Do not require root. [Microphone permission](https://support.apple.com/guide/mac-help/control-access-to-the-microphone-on-mac-mchla1b1e1fe/mac), [Input Monitoring permission](https://support.apple.com/guide/mac-help/control-access-to-input-monitoring-on-mac-mchl4cedafb6/mac), [pynput platform limitations](https://pynput.readthedocs.io/en/latest/limitations.html).

Own the following states in one controller:

`STARTING -> READY -> RECORDING -> TRANSCRIBING -> SUBMITTING -> READY`

Track server-side command jobs separately. Poll outstanding job IDs at a modest bounded interval (e.g. 500 ms), displaying transitions and final scores/results. Permit a new utterance after admission while earlier jobs are still running; this is the purpose of the queue. Keep capture/inference serialized locally. The stop key remains responsive in all connected states, including loading, recording, transcription, and submission.

- Press in `READY`: start one utterance and show `Listening`.
- Release in `RECORDING`: seal that utterance, process all accepted samples, then transcribe exactly once.
- Successful nonempty transcript: print/flush `Transcript: ...` before submission, allocate one command ID, and call `submit_voice_command` once (or the exact reserved stop path). Strip surrounding whitespace only; do not polish or rewrite the transcript.
- Successful admission: display the job ID and queued status; return to ready immediately. Later display validated scores, selected action, and dispatch/completion outcome from `get_voice_command`. Keep STT, queue-wait, model, and execution timings separate. In transcribe-only mode, return directly to ready after displaying text.
- Presses during local transcription/submission: show busy once and ignore the entire press/release gesture. Do not start recording automatically when the current request finishes while a key remains held. Waiting for an already-admitted job does not make the microphone busy.
- Escape while recording: discard that utterance. Escape does not stop the player. Terminal Ctrl-C shuts down the CLI from any state.
- Limit recording to 30 seconds. At the limit, discard with a clear message rather than submit an incomplete goal; require a fresh key press. This also bounds a missed key-up event.
- Silence, empty text, input overflow, device loss, or transcription failure: no command submission. Recover to ready if the hardware remains valid; otherwise stop cleanly with a useful error.
- Queue-full, MCP failure, provider failure, or invalid response: retain the displayed transcript and report the affected job. Do not create a second job to retry. If submission acknowledgement is lost, query the original ID before considering a retry with that same ID; a service restart or missing history is an uncertain outcome, not permission to replay.

Keep one utterance/inference/submission active per CLI, while status polling and stop requests remain independent. Reuse the MCP session and support concurrent tool calls. Cancellation must discard late local results; it cannot promise to cancel work already accepted by a remote provider or undo an already dispatched game action. Client disconnect must not trigger replay. Pending server jobs retain the documented bounded lifetime; an active bounded walk must still be stopped by the server worker.

**6. Audio and inference mechanics**

Use `sounddevice` for capture. Probe mono 16 kHz float32 support; if unsupported, capture at the device's supported native rate and convert. Support a stereo-only input by downmixing before resampling. Validate the actual format instead of assuming all Mac/Bluetooth devices accept 16 kHz mono.

For low press latency, open an input stream during initialization and keep it running until shutdown. Discard samples outside an active hold immediately; keep no pre-roll or idle audio history. Explain that the macOS microphone indicator remains active while the voice CLI runs. Retain only the current utterance in bounded RAM and clear it after recognition/cancellation. Do not write recordings to disk.

Use short capture blocks and a bounded, thread-safe handoff; callbacks do only minimal copying into preallocated storage and status signaling. Never perform inference, resampling, HTTP, logging, or blocking queue operations in an audio callback. Treat dropped audio as a failed utterance. [sounddevice stream requirements](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html).

At release, establish a capture cutoff and safely seal the buffer before inference. Ensure concurrent callbacks cannot append to the next utterance or omit accepted tail samples. Downmix if necessary, then resample the complete utterance once using `scipy.signal.resample_poly` with reduced integer up/down factors. Convert the result to contiguous mono float32 at the model's expected sample rate. [SciPy resampling API](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html).

Reject accidental taps below 150 ms and zero/near-silent input using a conservative, documented energy gate; test quiet speech so the gate is not overaggressive. This is not a promise of speech detection in arbitrary noise. Do not add a VAD model or infer intent from audio energy. Empty/whitespace model output must never become a queued command.

Use a single dedicated inference worker for model construction, warm-up, and subsequent inference. Keep blocking MLX calls off keyboard/audio callbacks and the controller event loop. Feed the complete waveform through the upstream in-memory preprocessing/generation path. Keep the model resident; discard per-utterance arrays/results. A timed-out or cancelled worker job must not allow a second concurrent inference on the same model. On shutdown close capture, stop/join listeners, discard unsent work, close HTTP clients, and finish worker cleanup.

**7. Performance gate and conditional streaming work**

The recommended acceptance target is warm **p95 release-to-transcript <= 750 ms for 2–10 second utterances on this Mac with Godot running**. This is a proposed product target, not an upstream performance guarantee. Separately record first-use load/warm-up, utterance duration, capture-start delay, release-to-text, queue wait, Jev/vision latency, and dispatch/completion latency. Do not include Gemini/Jev time in the STT latency claim or imply that fast transcription means instant game execution.

Collect at least 20 warm samples across 2-, 5-, and 10-second utterances; include a 30-second boundary check. Use the same scene/workload to compare game frame times without STT, during holding, and during inference. Record hardware, package versions, median/p95 latency, frame-time impact, and whether words/negations were preserved. Do not claim smooth coexistence without this check.

If complete-utterance inference meets the target and is accurate, keep it and do not add streaming. If it misses after eliminating cold-start/capture overhead, implement streaming inside the same STT boundary; keep the command contracts unchanged. Do not ship an unbenchmarked “fast” path merely because it returns partial text.

For that conditional implementation, create a fresh streaming context per utterance, serialize model access, aggregate roughly 250–500 ms chunks initially, bound backlog, and explicitly consume the tail before reading final text. Validate any end padding against last-word fixtures and the locked implementation; never invent an upstream `.finish()` call. If native-rate resampling is needed, use a stateful streaming resampler rather than independent chunk resampling. Do not routinely transcribe the entire utterance again on release, which would negate the latency benefit.

Require the same accuracy/latency/gameplay checks after switching. If neither local mode meets the target, report the measured limitation; do not silently introduce Groq, another model, or a paid service. Cloud STT is a separate future decision, with a different privacy/network contract.

**8. Files and dependency boundaries**

Keep the implementation small; suggested file ownership within `mcp_server_jev_client/`:

| File | Responsibility |
| --- | --- |
| `src/mcp_server_jev_client/voice.py` | CLI/configuration, capture state controller, output, MCP submission/status/control |
| `src/mcp_server_jev_client/voice_audio.py` | Device selection, capture buffer, format conversion |
| `src/mcp_server_jev_client/voice_stt.py` | Lazy local model loading, warm-up, serial inference boundary |
| `src/mcp_server_jev_client/voice_hotkey.py` | Small key listener wrapper and press/release normalization |
| `src/mcp_server_jev_client/providers.py`, `app.py` | Shared observation/scoring functions, provider lifecycle, preserved `/observe` |
| `src/mcp_server_jev_client/game_client.py` | Shared Godot HTTP transport with observe-only facade and executor capability |
| `src/mcp_server_jev_client/mcp_api.py` | Thin real MCP tool surface and session lifecycle |
| `src/mcp_server_jev_client/command_queue.py` | Job admission, bounded FIFO, deduplication, statuses, cancellation generations |
| `src/mcp_server_jev_client/executor.py` | Score/precondition checks, serialized dispatch, completion/stop handling |
| `src/mcp_server_jev_client/models.py` | New command receipt/status/scored-decision contracts alongside unchanged observation models |
| `pyproject.toml`, `uv.lock` | Voice extra, entry point, reproducible versions |
| `.env.example`, `README.md` | Correct game transport and voice setup/run/troubleshooting |
| `tests/test_voice*.py`, queue/executor/MCP tests, existing observation tests | Deterministic behavior, protocol, probability, and execution coverage |

Use `uv add --optional voice` to manage tested versions of `parakeet-mlx`, `sounddevice`, `pynput`, `numpy`, `scipy`, and `mlx` if directly imported. Mark Apple-only packages with `sys_platform == 'darwin' and platform_machine == 'arm64'`. Declare libraries directly imported rather than relying accidentally on transitive dependencies. Check resolution and runtime compatibility on Python 3.13/arm64; do not assume a permissive package metadata range proves compatibility. Keep voice imports out of `app.py` and package import side effects. Avoid a plugin framework or generic provider registry.

Keep `requirements.txt` delegating to the package. Standard installs stay lightweight; voice users explicitly install the extra. README examples must include `--extra voice` in voice commands so a later `uv run` does not remove optional packages.

**9. Tests and completion criteria**

Default tests use fake input/key sources, an injected recognizer, and mocked HTTP. They must run without a microphone, accessibility permissions, MLX, model downloads, network, GPU, or provider keys. Keep small injectable boundaries; do not build a general event framework for tests. Pure resampling tests can run with the voice extra separately, without loading MLX or opening devices.

Cover these outcomes:

- One held gesture yields exactly one transcript and one command admission with the exact stripped text; a valid scored command reaches the allowlisted executor once.
- Auto-repeat, duplicate releases, a second gesture while busy, silence, accidental taps, cancel, lost release/max duration, capture overflow, and model errors never create duplicate or unintended submissions.
- Cancellation/shutdown during capture and inference discards late results; a worker never overlaps inference calls. Model load and warm-up occur once.
- Native-rate/stereo conversion produces mono 16 kHz with correct duration, and release boundaries preserve the last samples without cross-utterance leakage.
- Admission/error/timeout paths preserve displayed text and the original command ID; failure recovery allows the next gesture without replaying an uncertain command.
- The observer checks HTTP and `ok`, rejects malformed envelopes/state/image, preserves original bytes/extra state, closes its client, and sends only the `observe` command. Existing pipeline timeout, cancellation, overlap, SDK serialization, and no-action tests still pass.
- Importing the normal app or requesting CLI help never opens hardware or downloads weights. Transcribe-only mode requires no provider keys and never calls MCP, the game, or providers.
- Real in-memory MCP initialization/list/call reaches the shared queue service; stop/status requests remain responsive during slow Jev inference. No tool settings are passed to Jev's System One API.
- Jev action and yaw distributions survive normalization; selected probability and distribution confidence remain distinct. Unknown labels, missing scores, NaN/invalid distributions, ties/low scores, unsupported compounds, and contradictory instructions do not execute.
- FIFO order, bounded capacity, queue expiry, same-ID deduplication, payload-conflict errors, cancellation generations, late inference results, service restart/lost history, and stale state are covered using an injected clock and fake game.
- `started` does not prematurely complete a rotation; grab/drop preconditions are enforced; walk always schedules its stop; `wait` never becomes a game command. Ambiguous dispatch timeouts pause execution rather than retry actions.
- Priority stop invalidates Python work, orders `clear_queue` before `stop`, and prevents late inference from restarting movement. Exact “stop” bypasses inference, while “do not stop” does not match that shortcut. Test stop races with scoring, dispatch, active rotation, and timed walking.

Document and run these commands from the Python package after implementation:

```sh
uv sync --locked
uv run --locked pytest -q
uv sync --locked --extra voice
uv run --locked --extra voice vod-voice --list-devices
uv run --locked --extra voice vod-voice --transcribe-only
```

For end-to-end verification, start rendered Godot from `game/` with `VOD_API_BIND=127.0.0.1`, configure provider keys and `GODOT_API_URL`, then run:

```sh
# Terminal 1, from mcp_server_jev_client/
uv run --locked --extra voice mcp-server-jev-client

# Terminal 2, from the same directory
uv run --locked --extra voice vod-voice
```

Using the extra in both terminals avoids concurrent uv commands reconciling the shared environment differently. Test with Godot focused and confirm transcript/admission output appears before Gemini/Jev completes. Verify “turn right ninety degrees” produces probabilities and an actual right turn; “walk forward” produces the documented bounded step; pickup/drop reflect game preconditions; and priority stop works while a model request is slow. Queue multiple utterances and verify order without duplicate execution. Include negation (“do not pick up the red crystal”), a compound instruction, a paused full thought, immediate release after the last word, silence, and repeated utterances. Test cached STT offline using transcribe-only mode. Preserve a separate regression proving `/observe` still never dispatches actions.

Implement in reviewable milestones: (1) typed score preservation and shared HTTP game client; (2) queue plus real MCP tools with a fake executor; (3) bounded executor and priority stop; (4) voice CLI; (5) live accuracy, latency, and gameplay checks. Do not call the full feature complete at milestone 2: scoring without execution does not meet the current working product assumption.

Completion requires passing mocked regressions plus a recorded manual hotkey/mic/game smoke test and measured performance. If permissions, credentials, or hardware access block manual verification, state exactly what is unverified; do not label it fully verified. Report the final architecture, changed files, tests, measured timing, and any remaining limitations. Do not commit or push.
