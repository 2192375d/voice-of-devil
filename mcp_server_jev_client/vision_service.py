"""Bounded background vision with exact-match, session-scoped summary reuse.

This service owns its HTTP and Gemini clients. Foreground requests always capture
new game state. Only speculative pending work can be replaced; requested frames
are never dropped or answered with an unrelated cached description.
"""
import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
from time import monotonic, perf_counter

from gemini_input import Observation
from observation import fetch_frame, observation_response
from gemini_quota import QuotaDeferredError, request_lane

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedFrame:
    frame: dict
    key: str
    epoch: int
    captured_at: float
    capture_ms: float
    capture_queue_ms: float


@dataclass(frozen=True)
class SummaryEntry:
    description: Observation
    source_sequence: int
    captured_at: float
    epoch: int
    queue_ms: float
    gemini_ms: float


@dataclass
class InferenceJob:
    lane: str
    started: bool = False
    waiters: int = 0


class ObservationSupersededError(ValueError):
    """The game restarted while the requested frame was being summarized."""


class VisionService:
    def __init__(self, vision, client, game_server: str, *, capture_interval=0.5,
                 max_cache_entries=16, cache_ttl=30.0, clock=monotonic,
                 background_enabled=False):
        if capture_interval <= 0 or max_cache_entries < 1 or cache_ttl <= 0:
            raise ValueError("Vision interval, cache capacity and TTL must be positive")
        self.vision = vision
        self.client = client
        self.game_server = game_server
        self.capture_interval = capture_interval
        self.background_enabled = background_enabled
        self._background_not_before = 0.0
        self.max_cache_entries = max_cache_entries
        self.cache_ttl = cache_ttl
        self._clock = clock
        self._cache: OrderedDict[str, SummaryEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._jobs: dict[asyncio.Task, InferenceJob] = {}
        self._pending: CapturedFrame | None = None
        self._pending_ready = asyncio.Event()
        self._capture_lock = asyncio.Lock()
        self._capture_waiters: set[asyncio.Task] = set()
        self._foreground_lane = asyncio.Semaphore(1)
        self._capture_task = None
        self._background_task = None
        self._epoch = 0
        self._last_sequence = None
        self._last_simulation_time = None
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Vision service is closed")

    def start(self):
        self._ensure_open()
        if self.background_enabled and self._capture_task is None:
            self._capture_task = asyncio.create_task(self._capture_loop(), name="vision-capture")
            self._background_task = asyncio.create_task(self._background_loop(), name="vision-background")
        return self

    async def __aenter__(self):
        return self.start()

    async def __aexit__(self, *exc):
        await self.aclose()

    def _key(self, frame):
        png = base64.b64decode(frame["image"]["data"], validate=True)
        if not png:
            raise ValueError("Observation image is empty")
        # Hash decoded bytes, not base64 formatting; hints are order-independent.
        # Sequence/time belong to the response, not Gemini's semantic input.
        context = json.dumps({
            "epoch": self._epoch,
            "vision": self.vision.cache_namespace,
            "hints": frame["result"].get("hints"),
            "png_sha256": hashlib.sha256(png).hexdigest(),
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(context.encode()).hexdigest()

    async def _capture(self):
        task = asyncio.current_task()
        self._capture_waiters.add(task)
        try:
            return await self._capture_frame()
        finally:
            self._capture_waiters.discard(task)

    async def _capture_frame(self):
        self._ensure_open()
        queued = perf_counter()
        # Godot serializes capture too. This also makes restart detection ordered.
        async with self._capture_lock:
            self._ensure_open()
            started = perf_counter()
            frame = await fetch_frame(self.client, self.game_server)
            self._ensure_open()
            captured_at = self._clock()
            capture_ms = (perf_counter() - started) * 1000
            state = frame["result"]
            sequence = state["observation_sequence"]
            simulation_time = state["simulation_time"]
            if self._last_sequence is not None and (
                sequence <= self._last_sequence or simulation_time < self._last_simulation_time
            ):
                self._epoch += 1
                self._cache.clear()
                self._pending = None
                logger.info("vision game restart detected; invalidated summary cache")
            self._last_sequence = sequence
            self._last_simulation_time = simulation_time
            return CapturedFrame(frame, self._key(frame), self._epoch, captured_at,
                                 capture_ms, (started - queued) * 1000)

    def _cached(self, key):
        now = self._clock()
        expired = [key for key, entry in self._cache.items()
                   if now - entry.captured_at >= self.cache_ttl]
        for old_key in expired:
            del self._cache[old_key]
        entry = self._cache.get(key)
        if entry is not None:
            self._cache.move_to_end(key)
        return entry

    async def _infer(self, captured, queued_at, job):
        # Exactly one foreground and one background inference can run at once.
        # Foreground work never acquires the background lane.
        if job.lane == "foreground":
            await self._foreground_lane.acquire()
        try:
            self._ensure_open()
            if captured.epoch != self._epoch:
                raise ObservationSupersededError("Game restarted before vision inference")
            job.started = True
            started = perf_counter()
            queue_ms = (started - queued_at) * 1000
            state = captured.frame["result"]
            lane_token = request_lane.set(job.lane)
            try:
                result = await self.vision.summarize_with_metrics(
                    captured.frame["image"]["data"], hints=state.get("hints"),
                    observation_sequence=state["observation_sequence"],
                )
            finally:
                request_lane.reset(lane_token)
            entry = SummaryEntry(result.description, state["observation_sequence"],
                                 captured.captured_at, captured.epoch, queue_ms,
                                 result.metrics.get("gemini_ms", (perf_counter() - started) * 1000))
            if captured.epoch != self._epoch:
                raise ObservationSupersededError("Game restarted during vision inference")
            # Failed/partial responses cannot reach this point. TTL is from source
            # capture, never extended by hits or by a slow cloud response.
            if self._clock() - entry.captured_at < self.cache_ttl:
                self._cache[captured.key] = entry
                self._cache.move_to_end(captured.key)
                while len(self._cache) > self.max_cache_entries:
                    self._cache.popitem(last=False)
            return entry
        finally:
            if job.lane == "foreground":
                self._foreground_lane.release()

    def _launch(self, captured, lane):
        job = InferenceJob(lane)
        task = asyncio.create_task(self._infer(captured, perf_counter(), job),
                                   name=f"vision-{lane}-inference")
        self._inflight[captured.key] = task
        self._jobs[task] = job

        def finished(completed):
            self._jobs.pop(completed, None)
            if self._inflight.get(captured.key) is completed:
                del self._inflight[captured.key]
            # Retrieve failures even if all foreground callers cancelled. Awaiters
            # still receive the original exception; failures are never cached.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    def _offer_background(self, captured):
        if captured.epoch != self._epoch:
            return
        if self._cached(captured.key) is not None or captured.key in self._inflight:
            # A newer already-covered frame makes an older pending frame obsolete.
            self._pending = None
            return
        self._pending = captured
        self._pending_ready.set()

    async def _capture_background_once(self):
        self._offer_background(await self._capture())

    async def _capture_loop(self):
        previous_error = None
        while True:
            started = perf_counter()
            try:
                await self._capture_background_once()
                previous_error = None
            except Exception as error:
                # Avoid flooding logs while the game is offline. No image/key data.
                if type(error) is not previous_error:
                    logger.warning("background capture failed: %s", type(error).__name__)
                    previous_error = type(error)
            await asyncio.sleep(max(0, self.capture_interval - (perf_counter() - started)))

    async def _background_loop(self):
        while True:
            await self._pending_ready.wait()
            # Wait before consuming the slot: newer captures replace pending work
            # throughout the cooldown, so the resumed job uses the newest frame.
            delay = self._background_not_before - self._clock()
            if delay > 0:
                await asyncio.sleep(delay)
            self._pending_ready.clear()
            captured, self._pending = self._pending, None
            if captured is None or captured.epoch != self._epoch:
                continue
            if self._cached(captured.key) is not None or captured.key in self._inflight:
                continue
            if any(job.lane == "foreground" for job in self._jobs.values()):
                continue
            try:
                await self._launch(captured, "background")
            except QuotaDeferredError as error:
                delay = error.retry_after_seconds if error.retry_after_seconds is not None else 60.0
                self._background_not_before = self._clock() + max(1, delay)
                logger.info("background vision deferred reason=%s retry_after_s=%.1f", error.reason, delay)
            except Exception as error:
                logger.warning("background vision failed: %s", type(error).__name__)

    async def observe(self):
        started = perf_counter()
        captured = await self._capture()
        ready = perf_counter()
        entry = self._cached(captured.key)
        cache_hit = entry is not None
        if entry is None:
            task = self._inflight.get(captured.key)
            if task is None:
                task = self._launch(captured, "foreground")
            # A caller timeout must not cancel inference shared by other callers.
            job = self._jobs[task]
            job.waiters += 1
            try:
                entry = await asyncio.shield(task)
            finally:
                job.waiters -= 1
                if not job.waiters and job.lane == "foreground" and not job.started and not task.done():
                    # Do not let timed-out requests build an orphaned cloud-call
                    # queue. Started work and still-shared work remain reusable.
                    if self._inflight.get(captured.key) is task:
                        del self._inflight[captured.key]
                    task.cancel()
        if captured.epoch != self._epoch:
            raise ObservationSupersededError("Game restarted while observing")
        finished = perf_counter()
        response = observation_response(
            captured.frame, entry.description, source_sequence=entry.source_sequence,
            cache_hit=cache_hit, source_age_ms=max(0, (self._clock() - entry.captured_at) * 1000),
        )
        response["timings_ms"] = {
            "capture": captured.capture_ms,
            "capture_queue": captured.capture_queue_ms,
            "queue": 0.0 if cache_hit else entry.queue_ms,
            "gemini": 0.0 if cache_hit else entry.gemini_ms,
            "vision_wait": (finished - ready) * 1000,
            "total": (finished - started) * 1000,
        }
        logger.info("observe sequence=%s source_sequence=%s cache_hit=%s capture_ms=%.2f "
                    "capture_queue_ms=%.2f queue_ms=%.2f gemini_ms=%.2f vision_wait_ms=%.2f total_ms=%.2f",
                    response["observation_sequence"], entry.source_sequence, cache_hit,
                    *response["timings_ms"].values())
        return response

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._pending = None
        tasks = {task for task in (self._capture_task, self._background_task,
                                   *self._inflight.values(), *self._capture_waiters)
                 if task is not None and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._jobs.clear()
        self._cache.clear()
        try:
            await self.client.aclose()
        finally:
            await self.vision.aclose()
