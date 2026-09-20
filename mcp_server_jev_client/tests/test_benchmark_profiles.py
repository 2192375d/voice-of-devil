import base64
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import benchmark_observe as benchmark
from gemini_input import Observation


DESCRIPTION = {"summary": "A held cup is at the bottom right.",
               "objects": [{"label": "cup", "screen_region": "right"}],
               "possible_hazards": ["pit"], "uncertainties": []}


def sample(sequence=1):
    return {"capture_ms": 15, "scene_categories": list(benchmark.SCENE_CATEGORIES),
            "expected_labels": ["cup"], "expected_absent_labels": ["sphere"],
            "expected_hazards": ["pit"], "expected_absent_hazards": ["fire"],
            "frame": {"ok": True, "result": {"status": "observed", "observation_sequence": sequence,
                                               "simulation_time": 1.0, "hints": {"objects": []}},
                      "image": {"mime_type": "image/png", "data": base64.b64encode(b"image" + bytes([sequence])).decode()}}}


def write_frames(tmp_path):
    for index in range(2):
        (tmp_path / f"frame_{index:04d}.json").write_text(json.dumps(sample(index + 1)))


def args(tmp_path, **kwargs):
    return SimpleNamespace(command="profiles", directory=tmp_path, output=tmp_path / "report.json",
                           profiles=["baseline", "optimized", "lite"], warm_calls=2, smoke=True, **kwargs)


def model_factory(monkeypatch, failure=False):
    models, calls = {}, []

    def factory(profile):
        counter = 0

        async def summarize(png, **kwargs):
            nonlocal counter
            counter += 1
            calls.append((profile, png, kwargs))
            if failure and profile == "lite" and counter == 2:
                error = ValueError("truncated")
                error.metrics = {"finish_reason": "MAX_TOKENS"}
                raise error
            return SimpleNamespace(description=Observation.model_validate(DESCRIPTION),
                                   metrics={"gemini_ms": 70, "output_tokens": 30,
                                            "thought_tokens": 0, "finish_reason": "STOP"})

        model = SimpleNamespace(summarize_with_metrics=AsyncMock(side_effect=summarize), aclose=AsyncMock())
        models[profile] = model
        return model

    monkeypatch.setattr(benchmark, "GeminiVision", factory)
    return models, calls


@pytest.mark.asyncio
async def test_profiles_use_identical_inputs_separate_cold_and_close(monkeypatch, tmp_path):
    write_frames(tmp_path)
    models, calls = model_factory(monkeypatch)
    await benchmark.run(args(tmp_path))
    report = json.loads((tmp_path / "report.json").read_text())
    for name in ("baseline", "optimized", "lite"):
        profile_calls = [call for call in calls if call[0] == name]
        assert [call[1] for call in profile_calls] == [sample(1)["frame"]["image"]["data"],
                                                      sample(1)["frame"]["image"]["data"],
                                                      sample(2)["frame"]["image"]["data"]]
        assert all(call[2]["hints"] == {"objects": []} for call in profile_calls)
        summary = report["summary"][name]
        assert summary["attempts"] == 2
        assert summary["cold"]["attempts"] == 1
        assert summary["recognition_errors"] == 0
        assert summary["thought_tokens"] == {"p50": 0, "p95": 0}
        assert not summary["quality_gate_passed"]
        models[name].aclose.assert_awaited_once()
    assert [call[0] for call in calls[6:]] == ["optimized", "lite", "baseline"]


@pytest.mark.asyncio
async def test_truncations_fail_and_stay_in_subsecond_denominator(monkeypatch, tmp_path):
    write_frames(tmp_path)
    model_factory(monkeypatch, failure=True)
    await benchmark.run(args(tmp_path))
    report = json.loads((tmp_path / "report.json").read_text())
    summary = report["summary"]["lite"]
    assert summary["successful"] == summary["failed"] == summary["truncated"] == 1
    assert summary["subsecond_success_rate"] == 0.5
    assert summary["gemini_ms"] == {"p50": 70, "p95": 70}
    assert summary["annotated_attempts"] == 1
    assert not summary["quality_gate_passed"]


def rows(count=100):
    records = []
    for attempt in range(count):
        for profile in ("baseline", "optimized"):
            records.append({"phase": "warm", "attempt": attempt, "profile": profile,
                            "complete_summary_ms": 700, "scene_categories": list(benchmark.SCENE_CATEGORIES),
                            "quality": benchmark.score_description(sample(), DESCRIPTION)})
    return records


def test_quality_gate_requires_annotations_categories_and_100_calls():
    records = rows()
    assert benchmark.summarize_profiles(records, ["baseline", "optimized"], 100)["optimized"]["quality_gate_passed"]
    assert not benchmark.summarize_profiles(records, ["baseline", "optimized"], 100, smoke=True)["optimized"]["quality_gate_passed"]
    records[1]["quality"] = None
    assert not benchmark.summarize_profiles(records, ["baseline", "optimized"], 100)["optimized"]["quality_gate_passed"]
    records = rows()
    for row in records:
        row["scene_categories"] = ["stationary"]
    result = benchmark.summarize_profiles(records, ["baseline", "optimized"], 100)["optimized"]
    assert "movement" in result["missing_scene_categories"]
    assert not result["quality_gate_passed"]
    assert not benchmark.summarize_profiles(rows(99), ["baseline", "optimized"], 99)["optimized"]["quality_gate_passed"]


def test_new_critical_error_rejected_even_if_total_error_count_improves():
    records = rows()
    records[0]["quality"]["recognition_errors"] = 3
    changed = copy.deepcopy(DESCRIPTION)
    changed["possible_hazards"] = []
    records[1]["quality"] = benchmark.score_description(sample(), changed)
    result = benchmark.summarize_profiles(records, ["baseline", "optimized"], 100)["optimized"]
    assert result["recognition_error_delta"] == -2
    assert result["new_critical_errors"] == 1
    assert not result["quality_gate_passed"]


def test_scoring_casefolds_and_flags_incorrect_labels_and_hazards():
    changed = copy.deepcopy(DESCRIPTION)
    changed["objects"][0]["label"] = "CUP"
    assert benchmark.score_description(sample(), changed)["recognition_errors"] == 0
    changed["objects"].append({"label": "sphere", "screen_region": "center"})
    changed["possible_hazards"].append("fire")
    result = benchmark.score_description(sample(), changed)
    assert result["incorrect_labels"] == ["sphere"]
    assert result["incorrect_hazards"] == ["fire"]
    assert len(result["critical_errors"]) == 2
    unannotated = sample()
    unannotated["expected_hazards"] = None
    assert benchmark.score_description(unannotated, DESCRIPTION) is None


@pytest.mark.asyncio
async def test_profiles_refuse_unqualified_short_run_or_existing_output_before_calls(monkeypatch, tmp_path):
    write_frames(tmp_path)
    models, calls = model_factory(monkeypatch)
    options = args(tmp_path)
    options.smoke = False
    with pytest.raises(ValueError, match="100"):
        await benchmark.run(options)
    assert not calls and not models
    options.smoke = True
    options.output.write_text("keep me")
    with pytest.raises(ValueError, match="already exists"):
        await benchmark.run(options)
    assert options.output.read_text() == "keep me"
    assert not calls and not models


@pytest.mark.asyncio
async def test_live_wait_reports_actual_queue_capture_hits_and_failures(monkeypatch, tmp_path):
    timing = {key: 5 for key in ("capture", "capture_queue", "queue", "gemini", "vision_wait", "total")}
    first = {"timings_ms": timing, "observation_sequence": 10,
             "vision": {"cache_hit": True, "source_age_ms": 600, "source_observation_sequence": 8}}
    second = copy.deepcopy(first)
    second["vision"]["cache_hit"] = False
    service = SimpleNamespace(start=lambda: None,
                              observe=AsyncMock(side_effect=[first, RuntimeError(), second]), aclose=AsyncMock())
    monkeypatch.setitem(sys.modules, "vision_service", SimpleNamespace(VisionService=lambda *a, **k: service))
    monkeypatch.setattr(benchmark, "GeminiVision", lambda **kwargs: object())
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: object())
    options = SimpleNamespace(command="observe-wait", output=tmp_path / "live.json", game_server="game:3000",
                              profile="optimized", count=3, interval=0, warmup=0, scene_category="movement")
    await benchmark.run(options)
    result = json.loads(options.output.read_text())
    summary = result["summary"]
    assert summary["failed"] == 1 and summary["successful"] == 2
    assert summary["cache_hit_rate"] == 1 / 3
    assert summary["subsecond_success_rate"] == 2 / 3
    assert summary["timings_ms"]["queue"] == {"p50": 5, "p95": 5}
    assert result["records"][0]["source_observation_sequence"] == 8
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_replay_stops_and_reports_quota_without_retry(monkeypatch, tmp_path):
    from gemini_quota import QuotaDeferredError
    write_frames(tmp_path)
    model = SimpleNamespace(
        summarize_with_metrics=AsyncMock(side_effect=QuotaDeferredError("minute_budget", 42)),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(benchmark, "GeminiVision", lambda **kwargs: model)
    options = args(tmp_path)
    await benchmark.run(options)
    report = json.loads(options.output.read_text())
    assert report["halted"] == {"reason": "minute_budget", "retry_after_seconds": 42}
    assert len(report["records"]) == 1
    assert report["records"][0]["deferred_reason"] == "minute_budget"
    assert report["records"][0]["retry_after_seconds"] == 42
    model.summarize_with_metrics.assert_awaited_once()
    assert not any(stats["quality_gate_passed"] for stats in report["summary"].values())
