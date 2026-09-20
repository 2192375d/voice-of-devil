"""Capture scenes, compare complete Gemini responses, or measure live vision waits.

capture is local/read-only gameplay. Other commands make paid Gemini calls.
Saved frames contain screenshots; reports contain model descriptions, never credentials.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
from time import perf_counter

import httpx

from gemini_input import GeminiVision
from observation import fetch_frame
from gemini_quota import QuotaDeferredError

PROFILE_NAMES = ("baseline", "optimized", "low", "lite")
SCENE_CATEGORIES = ("stationary", "movement", "occlusion", "held_item", "small_object", "wrong_label")


def percentiles(values):
    ordered = sorted(values)
    return {f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)]
            for p in (50, 95)} if ordered else {}


async def run(args):
    if args.command == "profiles":
        return await run_profiles(args)
    if args.command == "observe-wait":
        return await run_observe_wait(args)
    if args.command == "capture":
        args.directory.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=15) as client:
            for index in range(args.count):
                start = perf_counter()
                frame = await fetch_frame(client, args.game_server)
                capture_ms = (perf_counter() - start) * 1000
                path = args.directory / f"frame_{index:04d}.json"
                # Exclusive creation protects a previously annotated dataset.
                with path.open("x") as stream:
                    json.dump({"capture_ms": capture_ms, "frame": frame,
                               "scene_categories": [], "expected_labels": None,
                               "expected_absent_labels": [], "expected_hazards": None,
                               "expected_absent_hazards": []}, stream)
                if index + 1 < args.count:
                    await asyncio.sleep(args.interval)
        return

    paths = sorted(args.directory.glob("frame_*.json"))
    if not paths:
        raise ValueError("No captured frames found")
    model = GeminiVision()
    records = []
    try:
        for index, path in enumerate(paths):
            sample = json.loads(path.read_text())
            frame = sample["frame"]
            state = frame["result"]
            for hinted in ([False, True] if index % 2 == 0 else [True, False]):
                record = {"frame": path.name, "hinted": hinted, "capture_ms": sample["capture_ms"]}
                start = perf_counter()
                try:
                    result = await model.summarize(frame["image"]["data"],
                        hints=state.get("hints") if hinted else None,
                        observation_sequence=state["observation_sequence"])
                    record["gemini_ms"] = (perf_counter() - start) * 1000
                    record["capture_plus_gemini_ms"] = sample["capture_ms"] + record["gemini_ms"]
                    record["description"] = result.model_dump()
                    expected = sample.get("expected_labels")
                    if expected is not None:
                        actual = {obj.label.casefold() for obj in result.objects}
                        record["missing_labels"] = [s for s in expected if s.casefold() not in actual]
                        record["incorrect_labels"] = [s for s in sample.get("expected_absent_labels", [])
                                                      if s.casefold() in actual]
                except Exception as error:
                    record["error_type"] = type(error).__name__
                records.append(record)
    finally:
        await model.client.aio.aclose()
        model.client.close()
    summary = {}
    for hinted in (False, True):
        rows = [r for r in records if r["hinted"] == hinted]
        summary["hinted" if hinted else "image_only"] = {
            "successful": sum("gemini_ms" in r for r in rows),
            "failed": sum("error_type" in r for r in rows),
            "capture_plus_gemini_ms": percentiles([r["capture_plus_gemini_ms"] for r in rows
                                                  if "capture_plus_gemini_ms" in r]),
            "gemini_ms": percentiles([r["gemini_ms"] for r in rows if "gemini_ms" in r]),
            "annotated_frames": sum("missing_labels" in r for r in rows),
            "recognition_errors": sum(len(r.get("missing_labels", [])) + len(r.get("incorrect_labels", []))
                                      for r in rows) if any("missing_labels" in r for r in rows) else None,
        }
    with args.output.open("x") as stream:
        json.dump({"summary": summary, "records": records}, stream, indent=2)
    print(json.dumps(summary, indent=2))


def _normalized(values):
    return {value.strip().casefold() for value in values}


def score_description(sample, description):
    """Exact annotation checks, not a substitute for human semantic review."""
    expected = sample.get("expected_labels")
    hazards = sample.get("expected_hazards")
    if not isinstance(expected, list) or not isinstance(hazards, list):
        return None
    actual_labels = _normalized(obj["label"] for obj in description["objects"])
    actual_hazards = _normalized(description["possible_hazards"])
    missing_labels = _normalized(expected) - actual_labels
    incorrect_labels = _normalized(sample.get("expected_absent_labels", [])) & actual_labels
    missing_hazards = _normalized(hazards) - actual_hazards
    incorrect_hazards = _normalized(sample.get("expected_absent_hazards", [])) & actual_hazards
    # Unless annotated otherwise, all expected objects and hazards are critical.
    critical_labels = _normalized(sample.get("critical_labels", expected))
    critical_hazards = _normalized(sample.get("critical_hazards", hazards))
    critical_errors = ({"missing_label:" + value for value in missing_labels & critical_labels}
                       | {"incorrect_label:" + value for value in incorrect_labels}
                       | {"missing_hazard:" + value for value in missing_hazards & critical_hazards}
                       | {"incorrect_hazard:" + value for value in incorrect_hazards})
    return {"missing_labels": sorted(missing_labels), "incorrect_labels": sorted(incorrect_labels),
            "missing_hazards": sorted(missing_hazards), "incorrect_hazards": sorted(incorrect_hazards),
            "critical_errors": sorted(critical_errors),
            "recognition_errors": len(missing_labels | incorrect_labels) + len(missing_hazards | incorrect_hazards)}


def _record_error(record, error):
    record["error_type"] = type(error).__name__
    if isinstance(error, QuotaDeferredError):
        record["deferred_reason"] = error.reason
        record["retry_after_seconds"] = error.retry_after_seconds
        record["provider_status"] = error.provider_status
    metrics = getattr(error, "metrics", {})
    for field in ("gemini_ms", "input_tokens", "output_tokens", "thought_tokens", "total_tokens"):
        if field in metrics:
            record[field] = metrics[field]
    record["finish_reason"] = metrics.get("finish_reason", getattr(error, "finish_reason", None))
    record["truncated"] = record["finish_reason"] == "MAX_TOKENS"


def _latency_summary(rows, field="complete_summary_ms"):
    successes = [row for row in rows if "error_type" not in row]
    return {
        "attempts": len(rows), "successful": len(successes), "failed": len(rows) - len(successes),
        "truncated": sum(bool(row.get("truncated")) for row in rows),
        "deferred": sum("deferred_reason" in row for row in rows),
        field: percentiles([row[field] for row in successes]),
        # Failures and timeouts stay in the denominator, never become fast successes.
        "subsecond_success_rate": (sum(row[field] < 1000 for row in successes) / len(rows)
                                   if rows else None),
    }


def summarize_profiles(records, names, warm_calls, *, smoke=False):
    warm = [row for row in records if row["phase"] == "warm"]
    baseline = {row["attempt"]: row for row in warm if row["profile"] == "baseline"}
    summary = {}
    for name in names:
        rows = [row for row in warm if row["profile"] == name]
        cold = [row for row in records if row["phase"] == "cold" and row["profile"] == name]
        stats = _latency_summary(rows)
        stats["cold"] = _latency_summary(cold)
        successful = [row for row in rows if "error_type" not in row]
        for field in ("gemini_ms", "output_tokens", "thought_tokens", "capture_plus_summary_ms"):
            stats[field] = percentiles([row[field] for row in successful if row.get(field) is not None])
        annotated = [row for row in successful if row.get("quality") is not None]
        stats["annotated_attempts"] = len(annotated)
        stats["recognition_errors"] = (sum(row["quality"]["recognition_errors"] for row in annotated)
                                       if annotated else None)
        categories = {category for row in rows for category in row["scene_categories"]}
        stats["missing_scene_categories"] = sorted(set(SCENE_CATEGORIES) - categories)
        paired = [(row, baseline.get(row["attempt"])) for row in annotated]
        comparable = [(row, base) for row, base in paired
                      if base is not None and "error_type" not in base and base.get("quality") is not None]
        stats["new_critical_errors"] = sum(len(set(row["quality"]["critical_errors"])
                                               - set(base["quality"]["critical_errors"]))
                                            for row, base in comparable)
        stats["recognition_error_delta"] = sum(row["quality"]["recognition_errors"]
                                               - base["quality"]["recognition_errors"]
                                               for row, base in comparable)
        reasons = []
        if smoke:
            reasons.append("smoke run is not a qualification run")
        if warm_calls < 100 or len(rows) < 100:
            reasons.append("fewer than 100 warm attempts")
        if len(annotated) != len(rows):
            reasons.append("failed or unannotated attempts")
        if len(comparable) != len(rows):
            reasons.append("missing successful annotated baseline pairs")
        if stats["missing_scene_categories"]:
            reasons.append("missing representative scene categories")
        if stats["new_critical_errors"] or stats["recognition_error_delta"] > 0:
            reasons.append("quality regression against paired baseline")
        stats["quality_gate_passed"] = not reasons
        stats["quality_gate_reasons"] = reasons
        summary[name] = stats
    return summary


async def run_profiles(args):
    """Uncached replay; every candidate receives the same input for each attempt."""
    if args.output.exists():
        raise ValueError("output already exists")
    paths = sorted(args.directory.glob("frame_*.json"))
    if not paths:
        raise ValueError("No captured frames found")
    if args.warm_calls < 1 or (args.warm_calls < 100 and not args.smoke):
        raise ValueError("Use at least 100 warm calls, or --smoke for an unqualified smoke run")
    if len(set(args.profiles)) != len(args.profiles) or "baseline" not in args.profiles:
        raise ValueError("Profiles must be unique and include baseline")
    samples = [(path.name, json.loads(path.read_text())) for path in paths]
    models, records = {}, []
    halted = None
    try:
        for name in args.profiles:
            models[name] = GeminiVision(profile=name)
        for phase, count in (("cold", 1), ("warm", args.warm_calls)):
            for attempt in range(count):
                filename, sample = samples[attempt % len(samples)]
                frame, state = sample["frame"], sample["frame"]["result"]
                # Rotate and reverse to distribute the first/last request positions.
                order = list(args.profiles)
                if (attempt // len(order)) % 2:
                    order.reverse()
                offset = attempt % len(order)
                order = order[offset:] + order[:offset]
                for name in order:
                    row = {"profile": name, "phase": phase, "attempt": attempt, "frame": filename,
                           "scene_categories": sample.get("scene_categories", []),
                           "capture_ms": sample["capture_ms"]}
                    start = perf_counter()
                    try:
                        result = await models[name].summarize_with_metrics(frame["image"]["data"],
                            hints=state.get("hints"), observation_sequence=state["observation_sequence"])
                        row["complete_summary_ms"] = (perf_counter() - start) * 1000
                        row.update(result.metrics)
                        row["capture_plus_summary_ms"] = sample["capture_ms"] + row["complete_summary_ms"]
                        row["description"] = result.description.model_dump()
                        row["quality"] = score_description(sample, row["description"])
                    except Exception as error:
                        _record_error(row, error)
                        if isinstance(error, QuotaDeferredError):
                            halted = {"reason": error.reason, "retry_after_seconds": error.retry_after_seconds}
                    row["attempt_ms"] = (perf_counter() - start) * 1000
                    records.append(row)
                    if halted:
                        break
                if halted:
                    break
            if halted:
                break
    finally:
        for model in models.values():
            await model.aclose()
    summary = summarize_profiles(records, args.profiles, args.warm_calls, smoke=args.smoke)
    report = {"benchmark": "uncached_profile_replay", "smoke": args.smoke,
              "halted": halted,
              "warm_calls_per_profile": args.warm_calls,
              "notes": ["Cold means the first request on each new client, not a guaranteed cold remote model.",
                        "Replay capture-plus-summary is an estimate, not live vision wait or Jev/action latency.",
                        "Quality gates use exact annotations; manually review synonyms and unlisted hallucinations.",
                        "No production model/settings are changed by this benchmark."],
              "summary": summary, "records": records}
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(summary, indent=2))


async def run_observe_wait(args):
    """Measure actual capture-to-complete-vision waits, including cache/queue effects."""
    if args.output.exists():
        raise ValueError("output already exists")
    from vision_service import VisionService

    service = VisionService(GeminiVision(profile=args.profile), httpx.AsyncClient(timeout=15), args.game_server,
                            background_enabled=getattr(args, "background", False))
    records = []
    try:
        service.start()
        await asyncio.sleep(args.warmup)
        for attempt in range(args.count):
            row = {"attempt": attempt}
            start = perf_counter()
            try:
                result = await service.observe()
                row["observe_ms"] = (perf_counter() - start) * 1000
                row["timings_ms"] = result["timings_ms"]
                row["cache_hit"] = result["vision"]["cache_hit"]
                row["source_age_ms"] = result["vision"]["source_age_ms"]
                row["observation_sequence"] = result["observation_sequence"]
                row["source_observation_sequence"] = result["vision"]["source_observation_sequence"]
            except Exception as error:
                _record_error(row, error)
            row["attempt_ms"] = (perf_counter() - start) * 1000
            records.append(row)
            if attempt + 1 < args.count:
                await asyncio.sleep(args.interval)
    finally:
        await service.aclose()
    summary = _latency_summary(records, "observe_ms")
    successful = [row for row in records if "error_type" not in row]
    summary["cache_hits"] = sum(row["cache_hit"] for row in successful)
    summary["cache_hit_rate"] = summary["cache_hits"] / len(records) if records else None
    summary["timings_ms"] = {field: percentiles([row["timings_ms"][field] for row in successful])
                             for field in ("capture", "capture_queue", "queue", "gemini", "vision_wait", "total")}
    summary["cache_hit_observe_ms"] = percentiles([row["observe_ms"] for row in successful if row["cache_hit"]])
    summary["cache_miss_observe_ms"] = percentiles([row["observe_ms"] for row in successful if not row["cache_hit"]])
    with args.output.open("x") as stream:
        json.dump({"benchmark": "live_observe_wait", "profile": args.profile,
                   "background_enabled": getattr(args, "background", False),
                   "scene_category": args.scene_category, "warmup_seconds": args.warmup,
                   "notes": ["Move and change the scene during movement runs; this command sends no action commands.",
                             "Stationary cache hits alone do not establish subsecond changing-scene performance.",
                             "This measures vision delivery, excluding Jev decisions and action acknowledgments."],
                   "summary": summary, "records": records}, stream, indent=2)
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("directory", type=Path)
    capture.add_argument("--game-server", default="127.0.0.1:3000")
    capture.add_argument("--count", type=int, default=30)
    capture.add_argument("--interval", type=float, default=0.5)
    evaluate = commands.add_parser("evaluate", help="Makes paid Gemini calls")
    evaluate.add_argument("directory", type=Path)
    evaluate.add_argument("--output", type=Path, required=True)
    profiles = commands.add_parser("profiles", help="Paid uncached profile replay (404 calls by default)")
    profiles.add_argument("directory", type=Path)
    profiles.add_argument("--output", type=Path, required=True)
    profiles.add_argument("--profiles", nargs="+", choices=PROFILE_NAMES, default=list(PROFILE_NAMES))
    profiles.add_argument("--warm-calls", type=int, default=100)
    profiles.add_argument("--smoke", action="store_true", help="Allow fewer than 100 calls; cannot pass quality gate")
    observe = commands.add_parser("observe-wait", help="Paid live background + foreground vision benchmark")
    observe.add_argument("--output", type=Path, required=True)
    observe.add_argument("--game-server", default="127.0.0.1:3000")
    observe.add_argument("--profile", choices=PROFILE_NAMES, default="optimized")
    observe.add_argument("--count", type=int, default=100)
    observe.add_argument("--interval", type=float, default=0.5)
    observe.add_argument("--warmup", type=float, default=5)
    observe.add_argument("--background", action="store_true", help="Opt in to quota-limited background inference")
    observe.add_argument("--scene-category", choices=SCENE_CATEGORIES, required=True)
    args = parser.parse_args()
    if args.command == "capture" and (args.count < 1 or args.interval < 0):
        parser.error("count must be positive and interval nonnegative")
    if args.command != "capture" and args.output.exists():
        parser.error("output already exists")
    if args.command == "profiles":
        if args.warm_calls < 1 or (args.warm_calls < 100 and not args.smoke):
            parser.error("warm-calls must be >=100; use --smoke for smaller unqualified runs")
        if len(set(args.profiles)) != len(args.profiles) or "baseline" not in args.profiles:
            parser.error("profiles must be unique and include baseline")
    if args.command == "observe-wait" and (args.count < 1 or args.interval < 0 or args.warmup < 0):
        parser.error("count must be positive; interval and warmup must be nonnegative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
