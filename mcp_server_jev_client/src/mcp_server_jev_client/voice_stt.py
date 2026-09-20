"""Serial local Parakeet inference. Imports MLX only when loading."""

from __future__ import annotations

import threading
from typing import Any, Callable

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"
TARGET_RATE = 16_000


class SpeechRecognizer:
    def __init__(
        self,
        *,
        model_id: str = MODEL_ID,
        transcribe_fn: Callable[[Any], str] | None = None,
        loader: Callable[[], Any] | None = None,
    ):
        self.model_id = model_id
        self._transcribe_fn = transcribe_fn
        self._loader = loader
        self._lock = threading.Lock()
        self._model = None
        self.loads = 0
        self.warms = 0
        self.inferences = 0

    def ensure_ready(self, *, on_status: Callable[[str], None] | None = None) -> None:
        with self._lock:
            if self._model is not None or self._transcribe_fn is not None:
                return
            if on_status:
                on_status("Loading speech model (about 2.51 GB weights plus runtime memory)...")
            self._model = (self._loader or self._load_model)()
            self.loads += 1
            if on_status:
                on_status("Warming speech model...")
            self._infer_locked(self._silence())
            self.warms += 1

    def transcribe(self, samples: Any, *, cancelled: threading.Event | None = None) -> str | None:
        with self._lock:
            if cancelled is not None and cancelled.is_set():
                return None
            text = self._infer_locked(samples)
            if cancelled is not None and cancelled.is_set():
                return None
            return text

    def _load_model(self):
        from parakeet_mlx import from_pretrained

        return from_pretrained(self.model_id)

    def _silence(self):
        import numpy as np

        return np.zeros(TARGET_RATE // 2, dtype=np.float32)

    def _infer_locked(self, samples: Any) -> str:
        self.inferences += 1
        if self._transcribe_fn is not None:
            return self._transcribe_fn(samples)
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        audio = mx.array(samples)
        mel = get_logmel(audio, self._model.preprocessor_config)
        result = self._model.generate(mel)[0]
        return (result.text or "").strip()
