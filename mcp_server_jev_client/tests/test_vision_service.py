"""Offline, event-gated tests for freshness and bounded background vision."""
import asyncio
import base64
from collections import deque
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_input import Observation
from vision_service import ObservationSupersededError, VisionService
from gemini_quota import QuotaDeferredError, request_lane


PNG = b"\x89PNG\r\n\x1a\nopaque original image bytes"
HINTS = {"source": "godot", "objects": [{"label": "red sphere", "id": "7"}]}


def frame(sequence, *, image=PNG, hints=HINTS, simulation_time=None):
    return {
        "ok": True,
        "result": {
            "status": "observed", "observation_sequence": sequence,
            "simulation_time": sequence if simulation_time is None else simulation_time,
            "held_item": {"id": str(sequence)}, "hints": copy.deepcopy(hints),
            "future_field": {"retained": True},
        },
        "image": {"mime_type": "image/png", "data": base64.b64encode(image).decode()},
    }


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FrameFeed:
    def __init__(self, frames):
        self.frames = deque(frames)
        self.captured = asyncio.Queue()
        self.requests = []
        self.blocked = {}

    async def __call__(self, request):
        self.requests.append(json.loads(request.content))
        assert self.frames, "The service captured an unexpected extra frame"
        payload = self.frames.popleft()
        sequence = payload["result"]["observation_sequence"]
        self.captured.put_nowait(sequence)
        if sequence in self.blocked:
            await self.blocked[sequence].wait()
        return httpx.Response(200, json=payload)


class Model:
    def __init__(self):
        self.cache_namespace = "model/prompt/schema/settings-v1"
        self.started = asyncio.Queue()
        self.blocked = {}
        self.failures = {}
        self.cancelled = set()
        self.active = 0
        self.max_active = 0
        self.summarize_with_metrics = AsyncMock(side_effect=self._summarize)
        self.aclose = AsyncMock()

    def block(self, sequence):
        self.blocked[sequence] = asyncio.Event()
        return self.blocked[sequence]

    async def _summarize(self, png, *, hints=None, observation_sequence=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.put_nowait(observation_sequence)
        try:
            if observation_sequence in self.blocked:
                await self.blocked[observation_sequence].wait()
            if observation_sequence in self.failures:
                raise self.failures[observation_sequence]
            return SimpleNamespace(
                description=Observation(
                    summary=f"Scene from observation {observation_sequence}.",
                    objects=[{"label": "green object", "screen_region": "center"}],
                    possible_hazards=[], uncertainties=[],
                ),
                metrics={"gemini_ms": 123.0},
            )
        except asyncio.CancelledError:
            self.cancelled.add(observation_sequence)
            raise
        finally:
            self.active -= 1


@pytest.fixture
async def make_service():
    services = []

    def build(frames, **kwargs):
        kwargs.setdefault("background_enabled", True)
        model, feed, clock = Model(), FrameFeed(frames), Clock()
        client = httpx.AsyncClient(transport=httpx.MockTransport(feed))
        service = VisionService(model, client, "game:3000", clock=clock, **kwargs)
        services.append(service)
        return service, model, feed, clock

    yield build
    for service in services:
        await service.aclose()


async def next_started(model, expected):
    # This timeout only catches broken/deadlocked tests; no latency assertion.
    assert await asyncio.wait_for(model.started.get(), timeout=2) == expected


async def settle_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def test_background_is_opt_in(make_service):
    service, model, feed, _ = make_service([frame(1)], background_enabled=False)
    service.start()
    assert service._capture_task is service._background_task is None
    assert feed.requests == []
    await service.observe()
    assert model.summarize_with_metrics.await_count == 1


async def test_background_cooldown_keeps_latest_pending_frame(make_service, monkeypatch):
    service, model, _, clock = make_service([
        frame(1), frame(2, image=PNG + b"old pending"), frame(3, image=PNG + b"latest"),
    ])
    model.failures[1] = QuotaDeferredError("provider_429_cooldown", 60)
    sleeping, resume = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep

    async def controlled_sleep(seconds):
        if seconds >= 60:
            sleeping.set()
            await resume.wait()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    service._background_task = asyncio.create_task(service._background_loop())
    await service._capture_background_once()
    await next_started(model, 1)
    await settle_until(lambda: service._background_not_before > clock.now)
    await service._capture_background_once()
    await asyncio.wait_for(sleeping.wait(), 2)
    await service._capture_background_once()
    assert model.summarize_with_metrics.await_count == 1
    clock.advance(60)
    resume.set()
    await next_started(model, 3)
    assert model.summarize_with_metrics.await_count == 2


async def test_cache_remains_available_but_changed_frame_cannot_bypass_deferral(make_service):
    service, model, _, _ = make_service([frame(1), frame(2), frame(3, image=PNG + b"changed")])
    await service.observe()
    model.failures[3] = QuotaDeferredError("daily_budget", 3600)
    cached = await service.observe()
    assert cached["vision"]["cache_hit"] is True
    with pytest.raises(QuotaDeferredError, match="daily_budget"):
        await service.observe()
    assert model.summarize_with_metrics.await_count == 2


async def test_foreground_context_is_restored_on_error(make_service):
    service, model, _, _ = make_service([frame(1)])
    seen = []
    async def fail(*args, **kwargs):
        seen.append(request_lane.get())
        raise QuotaDeferredError("test")
    model.summarize_with_metrics.side_effect = fail
    with pytest.raises(QuotaDeferredError):
        await service.observe()
    assert seen == ["foreground"]
    assert request_lane.get() == "foreground"


async def test_exact_cache_returns_fresh_state_and_original_source(make_service):
    reordered_hints = {"objects": [{"id": "7", "label": "red sphere"}], "source": "godot"}
    service, model, feed, clock = make_service([frame(1), frame(2, hints=reordered_hints)])
    first = await service.observe()
    clock.advance(0.25)
    second = await service.observe()

    assert model.summarize_with_metrics.await_count == 1
    assert feed.requests == [{"command": "observe"}] * 2
    assert first["vision"]["cache_hit"] is False
    assert second["vision"]["cache_hit"] is True
    assert second["vision"]["source_observation_sequence"] == 1
    assert second["vision"]["source_age_ms"] == 250
    assert second["observation_sequence"] == second["simulation_time"] == 2
    assert second["game_state"] == {"held_item": {"id": "2"}, "future_field": {"retained": True}}
    assert second["vision"]["summary"] == first["vision"]["summary"]
    assert set(second["timings_ms"]) == {"capture", "capture_queue", "queue", "gemini", "vision_wait", "total"}
    assert all(value >= 0 for value in second["timings_ms"].values())
    assert second["timings_ms"]["gemini"] == second["timings_ms"]["queue"] == 0


@pytest.mark.parametrize("changed", ["image", "hints", "namespace"])
async def test_cache_requires_exact_semantic_input(make_service, changed):
    second = frame(2)
    if changed == "image":
        second = frame(2, image=PNG + b"one changed pixel")
    elif changed == "hints":
        second["result"]["hints"]["objects"][0]["label"] = "blue cube"
    service, model, _, _ = make_service([frame(1), second])
    await service.observe()
    if changed == "namespace":
        model.cache_namespace = "different-model-or-settings"
    result = await service.observe()
    assert model.summarize_with_metrics.await_count == 2
    assert result["vision"]["cache_hit"] is False
    assert result["vision"]["source_observation_sequence"] == 2


async def test_cache_expiration_is_from_original_capture_not_last_hit(make_service):
    service, model, _, clock = make_service([frame(1), frame(2), frame(3)])
    await service.observe()
    clock.advance(29)
    assert (await service.observe())["vision"]["source_age_ms"] == 29_000
    clock.advance(1)
    result = await service.observe()
    assert model.summarize_with_metrics.await_count == 2
    assert result["vision"]["cache_hit"] is False
    assert result["vision"]["source_observation_sequence"] == 3


async def test_inference_older_than_ttl_does_not_populate_cache(make_service):
    service, model, _, clock = make_service([frame(1), frame(2)])
    release = model.block(1)
    waiting = asyncio.create_task(service.observe())
    await next_started(model, 1)
    clock.advance(30)
    release.set()
    assert (await waiting)["vision"]["source_age_ms"] == 30_000
    assert (await service.observe())["vision"]["cache_hit"] is False
    assert model.summarize_with_metrics.await_count == 2


async def test_bounded_cache_evicts_least_recently_used_entry(make_service):
    service, model, _, _ = make_service(
        [frame(i, image=PNG + label) for i, label in enumerate([b"A", b"B", b"A", b"C", b"B"], 1)],
        max_cache_entries=2,
    )
    results = [await service.observe() for _ in range(5)]
    assert [item["vision"]["cache_hit"] for item in results] == [False, False, True, False, False]
    assert model.summarize_with_metrics.await_count == 4
    assert len(service._cache) == 2


@pytest.mark.parametrize("restart", [
    frame(1, simulation_time=11), frame(11, simulation_time=1), frame(10, simulation_time=11),
])
async def test_sequence_or_simulation_time_regression_invalidates_cache(make_service, restart):
    service, model, _, _ = make_service([frame(10), restart])
    await service.observe()
    result = await service.observe()
    assert result["vision"]["cache_hit"] is False
    assert model.summarize_with_metrics.await_count == 2


async def test_identical_inflight_requests_share_one_inference(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2)])
    release = model.block(1)
    first = asyncio.create_task(service.observe())
    await next_started(model, 1)
    second = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2)
    assert model.summarize_with_metrics.await_count == 1
    release.set()
    a, b = await asyncio.gather(first, second)
    assert model.summarize_with_metrics.await_count == 1
    assert a["vision"]["source_observation_sequence"] == b["vision"]["source_observation_sequence"] == 1
    assert a["observation_sequence"] == 1 and b["observation_sequence"] == 2


async def test_cancelling_one_waiter_does_not_cancel_shared_inference(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2)])
    release = model.block(1)
    first = asyncio.create_task(service.observe())
    await next_started(model, 1)
    second = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not model.cancelled
    release.set()
    assert (await second)["vision"]["source_observation_sequence"] == 1
    assert model.summarize_with_metrics.await_count == 1


async def test_unrelated_foreground_inferences_use_only_one_foreground_lane(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2, image=PNG + b"changed")])
    release = model.block(1)
    first = asyncio.create_task(service.observe())
    await next_started(model, 1)
    second = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2 and len(service._inflight) == 2)
    assert model.summarize_with_metrics.await_count == 1
    release.set()
    await asyncio.gather(first, second)
    assert model.max_active == 1


async def test_cancelled_queued_foreground_request_does_not_make_orphaned_api_call(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2, image=PNG + b"B")])
    release = model.block(1)
    first = asyncio.create_task(service.observe())
    await next_started(model, 1)
    queued = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2 and len(service._inflight) == 2)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await settle_until(lambda: len(service._inflight) == 1)
    release.set()
    await first
    assert model.summarize_with_metrics.await_count == 1


async def test_shared_queued_inference_survives_one_waiter_cancellation(make_service):
    service, model, feed, _ = make_service(
        [frame(1), frame(2, image=PNG + b"B"), frame(3, image=PNG + b"B")],
    )
    release = model.block(1)
    first = asyncio.create_task(service.observe())
    await next_started(model, 1)
    queued = asyncio.create_task(service.observe())
    survivor = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 3 and len(service._inflight) == 2)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(service._inflight) == 2
    release.set()
    await first
    result = await survivor
    assert result["vision"]["source_observation_sequence"] == 2
    assert result["observation_sequence"] == 3
    assert model.summarize_with_metrics.await_count == 2
    assert model.max_active == 1


@pytest.mark.parametrize("failure", [ValueError("truncated response"), TimeoutError("request timed out")])
async def test_failed_or_truncated_inference_is_not_cached(make_service, failure):
    service, model, _, _ = make_service([frame(1), frame(2)])
    model.failures[1] = failure
    with pytest.raises(type(failure), match=str(failure)):
        await service.observe()
    result = await service.observe()
    assert result["vision"]["cache_hit"] is False
    assert result["vision"]["source_observation_sequence"] == 2
    assert model.summarize_with_metrics.await_count == 2


async def test_foreground_miss_does_not_wait_behind_unrelated_background(make_service):
    service, model, _, _ = make_service([frame(1), frame(2, image=PNG + b"new frame")], capture_interval=3600)
    release = model.block(1)
    service.start()
    await next_started(model, 1)
    result = await asyncio.wait_for(service.observe(), timeout=2)
    assert not release.is_set()
    assert result["vision"]["source_observation_sequence"] == 2
    assert result["vision"]["cache_hit"] is False
    assert model.max_active == 2


async def test_foreground_identical_to_background_shares_inflight(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2)], capture_interval=3600)
    release = model.block(1)
    service.start()
    await next_started(model, 1)
    foreground = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2)
    assert model.summarize_with_metrics.await_count == 1
    release.set()
    result = await foreground
    assert result["vision"]["source_observation_sequence"] == 1
    assert result["observation_sequence"] == 2
    assert model.summarize_with_metrics.await_count == 1


async def test_background_replaces_only_speculative_pending_frame(make_service):
    service, model, _, _ = make_service(
        [frame(1), frame(2, image=PNG + b"B"), frame(3, image=PNG + b"C")], capture_interval=3600,
    )
    release = model.block(1)
    service.start()
    await next_started(model, 1)
    await service._capture_background_once()
    assert service._pending.frame["result"]["observation_sequence"] == 2
    await service._capture_background_once()
    assert service._pending.frame["result"]["observation_sequence"] == 3
    assert model.summarize_with_metrics.await_count == 1
    release.set()
    await next_started(model, 3)
    assert [call.kwargs["observation_sequence"] for call in model.summarize_with_metrics.await_args_list] == [1, 3]
    assert model.max_active == 1


async def test_game_restart_discards_old_pending_and_inflight_result(make_service):
    service, model, _, _ = make_service(
        [frame(10), frame(11, image=PNG + b"pending"), frame(1), frame(2)], capture_interval=3600,
    )
    release = model.block(10)
    service.start()
    await next_started(model, 10)
    await service._capture_background_once()
    assert service._pending is not None
    restarted = await asyncio.wait_for(service.observe(), timeout=2)
    assert restarted["vision"]["source_observation_sequence"] == 1
    assert service._pending is None
    release.set()
    await settle_until(lambda: not service._inflight)
    fresh = await service.observe()
    assert fresh["vision"]["cache_hit"] is True
    assert fresh["vision"]["source_observation_sequence"] == 1
    assert [call.kwargs["observation_sequence"] for call in model.summarize_with_metrics.await_args_list] == [10, 1]


async def test_foreground_frame_superseded_by_restart_fails_not_stale_success(make_service):
    service, model, feed, _ = make_service([frame(10), frame(1)])
    release = model.block(10)
    old = asyncio.create_task(service.observe())
    await next_started(model, 10)
    new = asyncio.create_task(service.observe())
    await settle_until(lambda: len(feed.requests) == 2)
    release.set()
    with pytest.raises(ObservationSupersededError):
        await old
    assert (await new)["vision"]["source_observation_sequence"] == 1


async def test_shutdown_cancels_inference_waiters_and_closes_owned_clients(make_service):
    service, model, _, _ = make_service([frame(1)])
    model.block(1)
    waiter = asyncio.create_task(service.observe())
    await next_started(model, 1)
    await service.aclose()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert model.cancelled == {1}
    assert service.client.is_closed
    model.aclose.assert_awaited_once()
    await service.aclose()
    model.aclose.assert_awaited_once()
    with pytest.raises(RuntimeError, match="closed"):
        await service.observe()


async def test_shutdown_cancels_hung_capture_and_capture_lock_waiter(make_service):
    service, model, feed, _ = make_service([frame(1), frame(2)])
    feed.blocked[1] = asyncio.Event()
    capture = asyncio.create_task(service.observe())
    assert await asyncio.wait_for(feed.captured.get(), timeout=2) == 1
    queued_capture = asyncio.create_task(service.observe())
    await settle_until(lambda: len(service._capture_waiters) == 2)
    try:
        await asyncio.wait_for(service.aclose(), timeout=2)
        assert capture.done() and queued_capture.done()
        results = await asyncio.gather(capture, queued_capture, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert len(feed.requests) == 1
        model.summarize_with_metrics.assert_not_awaited()
        assert service.client.is_closed
        model.aclose.assert_awaited_once()
    finally:
        for task in (capture, queued_capture):
            task.cancel()
        await asyncio.gather(capture, queued_capture, return_exceptions=True)


async def test_context_manager_starts_background_once_and_closes(make_service):
    service, model, feed, _ = make_service([frame(1)], capture_interval=3600)
    model.block(1)
    async with service:
        capture_task, background_task = service._capture_task, service._background_task
        service.start()
        assert service._capture_task is capture_task and service._background_task is background_task
        await next_started(model, 1)
        assert len(feed.requests) == 1
    assert capture_task.done() and background_task.done()
    assert service.client.is_closed
    model.aclose.assert_awaited_once()
