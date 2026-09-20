from __future__ import annotations

import argparse
import platform
import sys
from time import perf_counter

import numpy as np
import sounddevice as sd

TARGET_RATE = 16_000
MIN_SECONDS = 0.15
SILENCE_RMS = 0.0015  # RMS gate on float32 PCM in [-1, 1]; not a VAD
MAX_SECONDS = 30.0

# ---------------------------------------------------------------- audio

class Recorder:
    """Opens the mic only while recording, returns 16 kHz mono float32."""

    def __init__(self, device: str | None = None):
        self.device = int(device) if device and device.isdigit() else device
        self.rate, self.channels = self._probe()
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self.overflow = False
        self.truncated = False

    def _probe(self) -> tuple[int, int]:
        info = sd.query_devices(self.device, "input")
        native = int(info["default_samplerate"])
        stereo = min(2, int(info["max_input_channels"]))
        for rate, channels in ((TARGET_RATE, 1), (native, 1), (native, stereo)):
            try:
                sd.check_input_settings(
                    device=self.device, samplerate=rate, channels=channels, dtype="float32",
                )
                return rate, channels
            except Exception:
                continue
        raise RuntimeError("No usable input format found for this device")

    def start(self) -> None:
        self._chunks, self._frames = [], 0
        self.overflow = self.truncated = False
        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.rate,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status.input_overflow:
            self.overflow = True
        if self._frames >= MAX_SECONDS * self.rate:
            self.truncated = True
            return
        self._chunks.append(indata.copy())
        self._frames += frames

    def stop(self) -> np.ndarray:
        self.close()
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._chunks).mean(axis=1)
        if self.rate != TARGET_RATE:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(TARGET_RATE, self.rate)
            audio = resample_poly(audio, TARGET_RATE // g, self.rate // g)
        return np.ascontiguousarray(audio, dtype=np.float32)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


# ---------------------------------------------------------------- speech-to-text

def load_transcriber():
    """Return transcribe(samples) -> str. MLX on Apple Silicon, ONNX (CPU/CUDA) elsewhere."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            return _load_mlx()
        except ImportError:
            pass
    return _load_onnx()


def _load_mlx():
    import mlx.core as mx
    from parakeet_mlx import from_pretrained
    from parakeet_mlx.audio import get_logmel

    model = from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")

    def transcribe(samples: np.ndarray) -> str:
        mel = get_logmel(mx.array(samples), model.preprocessor_config)
        return (model.generate(mel)[0].text or "").strip()

    return transcribe


def _load_onnx():
    import onnx_asr

    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3")

    def transcribe(samples: np.ndarray) -> str:
        return str(model.recognize(samples)).strip()

    return transcribe


# ---------------------------------------------------------------- main loop

def process(samples: np.ndarray, rec: Recorder, transcribe) -> None:
    seconds = len(samples) / TARGET_RATE
    if rec.overflow:
        print("Audio was dropped; not submitting.")
    elif rec.truncated:
        print(f"Recording hit {MAX_SECONDS:g}s; discarded.")
    elif seconds < MIN_SECONDS:
        print(f"Tap ignored (under {MIN_SECONDS}s).")
    elif float(np.sqrt(np.mean(samples**2))) < SILENCE_RMS:
        print("Near-silent input; not submitting.")
    else:
        started = perf_counter()
        try:
            text = transcribe(samples)
        except Exception as error:
            print(f"Transcription failed: {error}")
            return
        if not text:
            print("Empty transcript; not submitting.")
            return
        print(f"Transcript: {text}  ({(perf_counter() - started) * 1000:.0f} ms)")
        return text


if __name__ == "__main__":
    rec = Recorder()
    print("Loading speech model (first run downloads weights)...")

    transcribe = load_transcriber()
    transcribe(np.zeros(TARGET_RATE // 2, dtype=np.float32))  # warm-up
    print("Ready.")

    try:
        while True:
            if input("\n[Enter] record, [q] quit > ").strip().lower() == "q":
                break
            rec.start()
            answer = input("Listening... [Enter] stop, [x] discard > ")
            samples = rec.stop()
            if answer.strip().lower() == "x":
                print("Discarded")
                continue
            process(samples, rec, transcribe)
    except (KeyboardInterrupt, EOFError):
        print("\nShutting down")
    finally:
        rec.close()

