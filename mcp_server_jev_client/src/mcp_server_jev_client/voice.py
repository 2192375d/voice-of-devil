"""Hold-to-talk CLI: local STT, MCP command submission, and status polling."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import threading
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4
from voice_stt import SpeechRecognizer

READY = "READY"
RECORDING = "RECORDING"
TRANSCRIBING = "TRANSCRIBING"
SUBMITTING = "SUBMITTING"
BUSY_STATES = {TRANSCRIBING, SUBMITTING}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vod-voice",
        description=(
            "Hold a key to speak a game command. Audio stays local; the transcript "
            "is queued for Jev scoring and bounded Godot execution."
        ),
    )
    parser.add_argument("--stop-key", default="f9", help="Immediate priority stop (default f9)")
    parser.add_argument("--device", default=None, help="Input device index or name")
    parser.add_argument("--list-devices", action="store_true", help="List input devices and exit")
    parser.add_argument(
        "--transcribe-only",
        action="store_true",
        help="Print transcripts without game, or provider calls",
    )
    return parser



def emit(message: str) -> None:
    print(message, flush=True)


@dataclass
class VoiceController:
    capture: Any
    recognizer: Any
    transcribe_only: bool = False
    poll_interval: float = 0.5
    max_utterance_seconds: float = 30.0
    loop: asyncio.AbstractEventLoop | None = None
    state: str = READY
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    _cancel: threading.Event = field(default_factory=threading.Event)
    _busy_warned: bool = False
    _limit_handle: asyncio.TimerHandle | None = None
    _poll_tasks: set[asyncio.Task] = field(default_factory=set)
    _work: asyncio.Task | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def post(self, kind: str) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.events.put_nowait, (kind, perf_counter()))

    async def run(self) -> None:
        while True:
            kind, received_at = await self.events.get()
            if kind == "shutdown":
                await self._shutdown()
                return
            if kind == "stop":
                await self._priority_stop()
            elif kind == "press":
                self._on_press()
            elif kind == "release":
                if self.state == RECORDING:
                    self._clear_limit()
                    self.state = TRANSCRIBING
                    utterance = self.capture.seal_utterance()
                    self._work = asyncio.create_task(
                        self._transcribe_and_submit(utterance, released_at=received_at),
                    )
            elif kind == "escape":
                self._on_escape()
            elif kind == "limit":
                self._on_limit()

    def _on_press(self) -> None:
        if self.state in BUSY_STATES:
            if not self._busy_warned:
                emit("Busy transcribing; release and press again after Ready.")
                self._busy_warned = True
            return
        if self.state != READY:
            return
        self._busy_warned = False
        self._cancel.clear()
        self.capture.begin_utterance()
        self.state = RECORDING
        emit("Listening")
        if self.loop is not None:
            self._limit_handle = self.loop.call_later(
                self.max_utterance_seconds, lambda: self.post("limit"),
            )

    def _on_escape(self) -> None:
        if self.state != RECORDING:
            return
        self._clear_limit()
        self.capture.seal_utterance()
        self._cancel.set()
        emit("Discarded utterance")
        self.state = READY

    def _on_limit(self) -> None:
        if self.state != RECORDING:
            return
        self.capture.seal_utterance()
        self._cancel.set()
        emit(f"Recording hit {self.max_utterance_seconds:g}s; discarded. Again.")
        self.state = READY

    def _clear_limit(self) -> None:
        if self._limit_handle is not None:
            self._limit_handle.cancel()
            self._limit_handle = None

    async def _transcribe_and_submit(self, utterance, *, released_at: float | None = None) -> None:
        if utterance.dropped:
            emit("Audio was dropped; not submitting.")
            self.state = READY
            return
        if utterance.overflow:
            emit("Recording overflowed; not submitting.")
            self.state = READY
            return
        if utterance.too_short:
            emit(f"Tap ignored (under {0.15}s).")
            self.state = READY
            return
        if utterance.silent:
            emit("Near-silent input; not submitting.")
            self.state = READY
            return
        started = perf_counter()
        try:
            text = await asyncio.to_thread(self.recognizer.transcribe, utterance.samples, cancelled=self._cancel)
            print("TEXT IS: ", text)
        except Exception as error:
            emit(f"Transcription failed: {error}")
            self.state = READY
            return
        stt_ms = (perf_counter() - started) * 1000
        if self._cancel.is_set():
            emit("Discarded late transcript")
            self.state = READY
            return
        if not text:
            emit("Empty transcript; not submitting.")
            self.state = READY
            return
        text = text.strip()
        if not text:
            emit("Empty transcript; not submitting.")
            self.state = READY
            return
        emit(f"Transcript: {text}")
        emit(f"STT: {stt_ms:.0f} ms")
        if released_at is not None:
            emit(f"Release-to-transcript: {(perf_counter() - released_at) * 1000:.0f} ms")
        if self.transcribe_only:
            self.state = READY
            return
        self.state = SUBMITTING
        command_id = uuid4()
        task = asyncio.create_task(self._poll(command_id), name=f"poll-{command_id}")
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)
        self.state = READY

    def _print_status(self, status) -> None:
        parts = [f"Job {status.command_id} {status.status}"]
        if status.source == "explicit_control":
            parts.append("source=explicit_control")
        if status.decision is not None:
            decision = status.decision
            parts.append(f"action={decision.action} p={decision.selected_action_probability:.2f}")
            if decision.distribution_confidence is not None:
                parts.append(f"confidence={decision.distribution_confidence:.2f}")
            if decision.action == "rotate":
                parts.append(f"yaw={decision.arguments.degrees.y}")
                if decision.selected_yaw_probability is not None:
                    parts.append(f"yaw_p={decision.selected_yaw_probability:.2f}")
        if status.execution and status.execution.interpretation:
            parts.append(status.execution.interpretation)
        if status.execution and status.execution.game_status:
            parts.append(f"game={status.execution.game_status}")
        timings = status.timings_ms or {}
        for name in ("queue_wait", "mcp", "gemini", "jev"):
            if name in timings:
                parts.append(f"{name}={timings[name]:.0f}ms")
        if status.error:
            parts.append(f"error={status.error}")
        emit(" ".join(parts))

    async def _priority_stop(self) -> None:
        if self.state == RECORDING:
            self._clear_limit()
            self.capture.seal_utterance()
            self.state = READY
        if self.state in BUSY_STATES:
            self._cancel.set()

    async def _shutdown(self) -> None:
        self._cancel.set()
        self._clear_limit()
        if self._work is not None:
            try:
                await self._work
            except asyncio.CancelledError:
                pass
            self._work = None
        for task in list(self._poll_tasks):
            task.cancel()


async def _async_main(args: argparse.Namespace) -> None:
    from voice_audio import MicrophoneCapture

    capture = MicrophoneCapture(args.device)
    recognizer = SpeechRecognizer()
    controller = VoiceController(
        capture=capture,
        recognizer=recognizer,
        transcribe_only=args.transcribe_only,
    )
    loop = asyncio.get_running_loop()
    controller.bind_loop(loop)

    def talk_press() -> None:
        controller.post("press")

    def talk_release() -> None:
        controller.post("release")

    def stop() -> None:
        controller.post("stop")

    def escape() -> None:
        controller.post("escape")

    emit("Opening microphone (the macOS indicator stays on while this CLI runs)...")
    capture.start_stream()
    emit("Loading...")
    try:
        await asyncio.to_thread(recognizer.ensure_ready, on_status=emit)
        emit(f"Ready — talk")
        await controller.run()
    finally:
        capture.close()
        await controller._shutdown()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_devices:
        from voice_audio import list_input_devices

        for line in list_input_devices():
            emit(line)
        return
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        emit("Shutting down")

if __name__ == "__main__":
    main()