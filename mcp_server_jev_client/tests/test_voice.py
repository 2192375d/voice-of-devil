"""Voice CLI controller tests with fake capture, recognizer, and MCP client."""

import asyncio
import sys
import threading
from uuid import UUID, uuid4

import pytest

from mcp_server_jev_client.models import StopGameResult, VoiceCommandReceipt, VoiceCommandStatus
from mcp_server_jev_client.voice import VoiceController, build_parser
from mcp_server_jev_client.voice_audio import SealedUtterance, rms_energy
from mcp_server_jev_client.voice_stt import SpeechRecognizer


def spoken(duration=1.0, rms=0.1, dropped=False, overflow=False):
    samples = [0.1] * int(16000 * duration)
    return SealedUtterance(
        samples=samples,
        sample_rate=16000,
        duration_seconds=duration,
        rms=rms,
        dropped=dropped,
        overflow=overflow,
    )


class FakeCapture:
    def __init__(self, utterance=None):
        self.utterance = utterance or spoken()
        self.begins = 0
        self.seals = 0

    def begin_utterance(self):
        self.begins += 1

    def seal_utterance(self):
        self.seals += 1
        return self.utterance


class FakeRecognizer:
    def __init__(self, text="turn right ninety degrees", delay=0.0, error=None):
        self.text = text
        self.delay = delay
        self.error = error
        self.calls = []
        self.loads = 0
        self.warms = 0
        self.entered = threading.Event()

    def transcribe(self, samples, cancelled=None):
        self.entered.set()
        self.calls.append(samples)
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if cancelled is not None and cancelled.is_set():
            return None
        return self.text


class FakeClient:
    def __init__(self, accept=True, error=None, status="queued"):
        self.submitted = []
        self.stops = 0
        self.closed = 0
        self.accept = accept
        self.error = error
        self.status = status
        self.jobs = {}

    async def submit(self, command_id, transcript):
        self.submitted.append((command_id, transcript))
        if self.error:
            raise self.error
        receipt = VoiceCommandReceipt(
            accepted=self.accept,
            service_instance_id=uuid4(),
            command_id=command_id,
            status=self.status if self.accept else None,
            error=None if self.accept else "queue_full",
        )
        self.jobs[command_id] = VoiceCommandStatus(
            found=True,
            service_instance_id=receipt.service_instance_id,
            command_id=command_id,
            transcript=transcript,
            status="completed" if self.accept else None,
            source="explicit_control" if transcript.strip().casefold() == "stop" else "jev",
        )
        return receipt

    async def get(self, command_id):
        return self.jobs.get(command_id) or VoiceCommandStatus(
            found=False, service_instance_id=uuid4(), command_id=command_id, error="unknown_command",
        )

    async def stop_game(self):
        self.stops += 1
        return StopGameResult(
            service_instance_id=uuid4(),
            cancelled_jobs=0,
            generation=1,
            game_cleared=True,
            game_stopped=True,
            execution_paused=False,
        )

    async def close(self):
        self.closed += 1


async def drive(controller, *kinds):
    task = asyncio.create_task(controller.run())
    for kind in kinds:
        controller.post(kind)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    controller.post("shutdown")
    await asyncio.wait_for(task, 2)


@pytest.fixture
async def controller_bundle():
    capture = FakeCapture()
    recognizer = FakeRecognizer()
    client = FakeClient()
    controller = VoiceController(capture, recognizer, client, poll_interval=0.01)
    controller.bind_loop(asyncio.get_running_loop())
    return controller, capture, recognizer, client


async def test_one_gesture_submits_stripped_transcript_once(controller_bundle):
    controller, capture, recognizer, client = controller_bundle
    recognizer.text = "  turn right ninety degrees  "
    await drive(controller, "press", "release")
    assert capture.begins == capture.seals == 1
    assert len(recognizer.calls) == 1
    assert len(client.submitted) == 1
    assert client.submitted[0][1] == "turn right ninety degrees"


async def test_auto_repeat_and_duplicate_release_do_not_double_submit(controller_bundle):
    controller, capture, recognizer, client = controller_bundle
    await drive(controller, "press", "press", "release", "release")
    assert capture.begins == 1
    assert len(client.submitted) == 1


async def test_busy_second_gesture_is_ignored():
    capture = FakeCapture()
    recognizer = FakeRecognizer(delay=0.2)
    client = FakeClient()
    controller = VoiceController(capture, recognizer, client)
    controller.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(controller.run())
    controller.post("press")
    await asyncio.sleep(0)
    controller.post("release")
    await asyncio.sleep(0.05)
    controller.post("press")
    controller.post("release")
    await asyncio.sleep(0.3)
    controller.post("shutdown")
    await task
    assert len(recognizer.calls) == 1
    assert len(client.submitted) == 1


async def test_silence_tap_overflow_and_model_error_never_submit():
    client = FakeClient()
    rec = FakeRecognizer()

    async def once(utterance, error=None):
        capture = FakeCapture(utterance)
        recognizer = FakeRecognizer(error=error) if error else rec
        controller = VoiceController(capture, recognizer, client)
        controller.bind_loop(asyncio.get_running_loop())
        await drive(controller, "press", "release")

    await once(spoken(rms=0.0))
    await once(spoken(duration=0.05, rms=0.2))
    await once(spoken(overflow=True))
    await once(spoken(), error=RuntimeError("model"))
    assert client.submitted == []


async def test_escape_and_max_duration_discard():
    client = FakeClient()
    capture = FakeCapture()
    controller = VoiceController(capture, FakeRecognizer(), client, max_utterance_seconds=30)
    controller.bind_loop(asyncio.get_running_loop())
    await drive(controller, "press", "escape")
    assert client.submitted == []
    capture = FakeCapture()
    controller = VoiceController(capture, FakeRecognizer(), client)
    controller.bind_loop(asyncio.get_running_loop())
    await drive(controller, "press", "limit")
    assert client.submitted == []


async def test_transcribe_only_never_calls_client():
    client = FakeClient()
    controller = VoiceController(FakeCapture(), FakeRecognizer(), client, transcribe_only=True)
    controller.bind_loop(asyncio.get_running_loop())
    await drive(controller, "press", "release")
    assert client.submitted == []
    assert client.stops == 0


async def test_failure_keeps_original_id_and_allows_next_gesture():
    client = FakeClient(accept=False)
    rec = FakeRecognizer(text="walk forward")
    controller = VoiceController(FakeCapture(), rec, client)
    controller.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(controller.run())
    controller.post("press")
    await asyncio.sleep(0)
    controller.post("release")
    await asyncio.sleep(0.05)
    first_id = client.submitted[0][0]
    controller.post("press")
    await asyncio.sleep(0)
    controller.post("release")
    await asyncio.sleep(0.05)
    controller.post("shutdown")
    await task
    assert [transcript for _, transcript in client.submitted] == ["walk forward", "walk forward"]
    assert client.submitted[0][0] == first_id
    assert client.submitted[1][0] != first_id


async def test_late_cancel_discards_transcript():
    rec = FakeRecognizer(delay=0.15)
    client = FakeClient()
    controller = VoiceController(FakeCapture(), rec, client)
    controller.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(controller.run())
    controller.post("press")
    await asyncio.sleep(0)
    controller.post("release")
    await asyncio.sleep(0.02)
    controller._cancel.set()
    await asyncio.sleep(0.2)
    controller.post("shutdown")
    await task
    assert client.submitted == []


async def test_stop_key_during_transcription_does_not_submit():
    rec = FakeRecognizer(delay=0.15)
    client = FakeClient()
    controller = VoiceController(FakeCapture(), rec, client)
    controller.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(controller.run())
    controller.post("press")
    await asyncio.sleep(0)
    controller.post("release")
    await asyncio.sleep(0.02)
    controller.post("stop")
    await asyncio.sleep(0.2)
    controller.post("shutdown")
    await task
    assert client.stops == 1
    assert client.submitted == []


def test_recognizer_serializes_and_loads_once():
    import time

    active = 0
    max_active = 0
    guard = threading.Lock()

    def transcribe(samples):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return "ok"

    recognizer = SpeechRecognizer(transcribe_fn=transcribe)
    threads = [threading.Thread(target=lambda: recognizer.transcribe([0.1])) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max_active == 1
    recognizer.ensure_ready()
    assert recognizer.loads == 0


def test_help_and_app_import_do_not_load_voice_backends():
    script = (
        "import sys\n"
        "from mcp_server_jev_client.voice import build_parser\n"
        "from mcp_server_jev_client import app\n"
        "build_parser().format_help()\n"
        "assert 'parakeet_mlx' not in sys.modules\n"
        "assert 'sounddevice' not in sys.modules\n"
        "assert 'pynput' not in sys.modules\n"
        "assert 'mcp_server_jev_client.voice_stt' not in sys.modules\n"
    )
    import subprocess
    completed = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    assert completed.returncode == 0


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.mcp_url.endswith("/mcp")
    assert args.hotkey == "f8"
    assert args.stop_key == "f9"


def test_rms_energy_empty_is_zero():
    assert rms_energy([]) == 0
    assert rms_energy([0.0, 0.0]) == 0


def test_resample_native_rate_and_stereo():
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    from mcp_server_jev_client.voice_audio import downmix_and_resample, TARGET_RATE

    seconds = 0.25
    stereo = np.ones((int(48000 * seconds), 2), dtype=np.float32)
    mono = downmix_and_resample(stereo, 48000, TARGET_RATE)
    assert mono.ndim == 1
    assert mono.dtype == np.float32
    assert abs(len(mono) / TARGET_RATE - seconds) < 0.02
    same = downmix_and_resample(np.ones(TARGET_RATE, dtype=np.float32), TARGET_RATE, TARGET_RATE)
    assert len(same) == TARGET_RATE


def test_capture_buffer_keeps_tail_and_does_not_leak():
    np = pytest.importorskip("numpy")
    from mcp_server_jev_client.voice_audio import CaptureBuffer

    buf = CaptureBuffer(20, 1)
    buf.begin()
    buf.append(np.ones((8, 1), dtype=np.float32))
    buf.append(np.full((5, 1), 3, dtype=np.float32))
    data, dropped, overflow = buf.seal()
    assert not dropped and not overflow
    assert len(data) == 13
    assert data[-1, 0] == 3
    buf.begin()
    empty, _, _ = buf.seal()
    assert len(empty) == 0
    buf.begin()
    buf.append(np.ones((30, 1), dtype=np.float32))
    data, dropped, overflow = buf.seal()
    assert overflow and len(data) == 20


async def test_do_not_stop_is_submitted_not_treated_as_emergency():
    client = FakeClient()
    rec = FakeRecognizer(text="do not stop")
    controller = VoiceController(FakeCapture(), rec, client)
    controller.bind_loop(asyncio.get_running_loop())
    await drive(controller, "press", "release")
    assert client.submitted[0][1] == "do not stop"
    assert client.stops == 0
    assert client.jobs[client.submitted[0][0]].source == "jev"


def test_hotkey_ignores_auto_repeat():
    pynput = pytest.importorskip("pynput")
    from mcp_server_jev_client.voice_hotkey import HotkeyListener

    events = []
    listener = HotkeyListener(
        "f8",
        "f9",
        on_talk_press=lambda: events.append("press"),
        on_talk_release=lambda: events.append("release"),
        on_stop=lambda: events.append("stop"),
        on_escape=lambda: events.append("escape"),
    )
    key = pynput.keyboard.Key.f8
    listener._on_press(key)
    listener._on_press(key)
    listener._on_release(key)
    listener._on_release(key)
    listener._on_press(pynput.keyboard.Key.f9)
    assert events == ["press", "release", "stop"]


async def test_release_latency_includes_sealing_and_resampling(monkeypatch, capsys):
    from mcp_server_jev_client import voice

    now = [10.0]
    monkeypatch.setattr(voice, "perf_counter", lambda: now[0])

    class SlowSealCapture(FakeCapture):
        def seal_utterance(self):
            now[0] += 0.4
            return super().seal_utterance()

    class TimedRecognizer(FakeRecognizer):
        def transcribe(self, samples, cancelled=None):
            now[0] += 0.1
            return "turn right"

    controller = VoiceController(SlowSealCapture(), TimedRecognizer(), transcribe_only=True)
    controller.bind_loop(asyncio.get_running_loop())
    await drive(controller, "press", "release")
    output = capsys.readouterr().out
    assert "STT: 100 ms" in output
    assert "Release-to-transcript: 500 ms" in output
