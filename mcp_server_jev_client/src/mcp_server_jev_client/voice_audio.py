"""Microphone capture, format conversion, and utterance sealing."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

TARGET_RATE = 16_000
MIN_UTTERANCE_SECONDS = 0.15
# Conservative RMS gate on float32 PCM in [-1, 1]. Quiet speech should pass;
# this is not a VAD and does not infer intent from energy.
SILENCE_RMS = 0.0005
MAX_UTTERANCE_SECONDS = 30.0
BLOCKSIZE = 256


@dataclass
class SealedUtterance:
    samples: Any
    sample_rate: int
    duration_seconds: float
    rms: float
    dropped: bool = False
    overflow: bool = False

    @property
    def too_short(self) -> bool:
        return self.duration_seconds < MIN_UTTERANCE_SECONDS

    @property
    def silent(self) -> bool:
        return self.rms < SILENCE_RMS


def rms_energy(samples: Any) -> float:
    if samples is None:
        return 0.0
    length = len(samples)
    if length == 0:
        return 0.0
    if hasattr(samples, "mean"):
        squared = samples * samples
        return float(squared.mean() ** 0.5)
    return math.sqrt(sum(float(x) * float(x) for x in samples) / length)


def downmix_and_resample(frames: Any, source_rate: int, target_rate: int = TARGET_RATE):
    import numpy as np
    from scipy.signal import resample_poly

    audio = np.asarray(frames, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    audio = np.ascontiguousarray(audio.reshape(-1), dtype=np.float32)
    if source_rate == target_rate:
        return audio
    gcd = math.gcd(int(target_rate), int(source_rate))
    up = int(target_rate) // gcd
    down = int(source_rate) // gcd
    return np.ascontiguousarray(resample_poly(audio, up, down).astype(np.float32))


class CaptureBuffer:
    def __init__(self, max_frames: int, channels: int):
        import numpy as np

        self._data = np.zeros((max_frames, channels), dtype=np.float32)
        self._n = 0
        self._lock = threading.Lock()
        self._active = False
        self._dropped = False
        self._overflow = False

    def begin(self) -> None:
        with self._lock:
            self._n = 0
            self._dropped = False
            self._overflow = False
            self._active = True

    def append(self, frames) -> None:
        with self._lock:
            if not self._active:
                return
            incoming = len(frames)
            take = min(incoming, len(self._data) - self._n)
            if take:
                self._data[self._n : self._n + take] = frames[:take]
                self._n += take
            if take < incoming:
                self._overflow = True
                self._active = False

    def mark_dropped(self) -> None:
        with self._lock:
            if self._active:
                self._dropped = True
                self._active = False

    def seal(self):
        with self._lock:
            self._active = False
            n = self._n
            dropped = self._dropped
            overflow = self._overflow
            copy = self._data[:n].copy()
            self._n = 0
        return copy, dropped, overflow


def list_input_devices() -> list[str]:
    import sounddevice as sd

    devices = sd.query_devices()
    lines = []
    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        lines.append(f"{index}: {device['name']} ({device['max_input_channels']} ch, {int(device['default_samplerate'])} Hz)")
    return lines


def resolve_device(spec: str | int | None):
    import sounddevice as sd

    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    name = str(spec).strip().lower()
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0 and name in device["name"].lower():
            return index
    raise RuntimeError(f"No input device matching {spec!r}")


def probe_input_format(device=None) -> tuple[int, int]:
    import sounddevice as sd

    try:
        sd.check_input_settings(device=device, samplerate=TARGET_RATE, channels=1, dtype="float32")
        return TARGET_RATE, 1
    except Exception:
        info = sd.query_devices(device, "input")
        channels = 1 if info["max_input_channels"] >= 1 else info["max_input_channels"]
        if info["max_input_channels"] >= 1:
            try:
                sd.check_input_settings(
                    device=device, samplerate=info["default_samplerate"], channels=1, dtype="float32",
                )
                return int(info["default_samplerate"]), 1
            except Exception:
                pass
        channels = min(2, int(info["max_input_channels"]))
        sd.check_input_settings(
            device=device, samplerate=info["default_samplerate"], channels=channels, dtype="float32",
        )
        return int(info["default_samplerate"]), channels


class MicrophoneCapture:
    def __init__(self, device=None):
        import sounddevice as sd

        self.device = resolve_device(device)
        self.native_rate, self.channels = probe_input_format(self.device)
        max_frames = int(MAX_UTTERANCE_SECONDS * self.native_rate) + BLOCKSIZE
        self.buffer = CaptureBuffer(max_frames, self.channels)
        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.native_rate,
            channels=self.channels,
            dtype="float32",
            blocksize=BLOCKSIZE,
            callback=self._callback,
        )

    def start_stream(self) -> None:
        self._stream.start()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def begin_utterance(self) -> None:
        self.buffer.begin()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status and getattr(status, "input_overflow", False):
            self.buffer.mark_dropped()
            return
        self.buffer.append(indata)

    def seal_utterance(self) -> SealedUtterance:
        raw, dropped, overflow = self.buffer.seal()
        converted = downmix_and_resample(raw, self.native_rate, TARGET_RATE)
        duration = len(converted) / TARGET_RATE
        return SealedUtterance(
            samples=converted,
            sample_rate=TARGET_RATE,
            duration_seconds=duration,
            rms=rms_energy(converted),
            dropped=dropped,
            overflow=overflow,
        )
