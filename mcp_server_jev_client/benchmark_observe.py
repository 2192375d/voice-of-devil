"""Capture once, then compare Gemini with/without hints on identical frames.

capture is local/read-only gameplay. evaluate makes two paid Gemini calls per frame.
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


def percentiles(values):
    ordered = sorted(values)
    return {f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)]
            for p in (50, 95)} if ordered else {}


async def run(args):
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
                               "expected_labels": None, "expected_absent_labels": []}, stream)
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
    args = parser.parse_args()
    if args.command == "capture" and (args.count < 1 or args.interval < 0):
        parser.error("count must be positive and interval nonnegative")
    if args.command == "evaluate" and args.output.exists():
        parser.error("output already exists")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
