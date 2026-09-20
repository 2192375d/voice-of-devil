import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.errors import ClientError, ServerError
import pytest

from gemini_input import GeminiVision
from gemini_quota import (DAY, QuotaDeferredError, QuotaGovernor, QuotaLimits,
                          provider_cooldown, request_lane)


class Clock:
    now = 1000000.0

    def __call__(self):
        return self.now


@pytest.fixture
def guard(tmp_path):
    return QuotaGovernor(tmp_path / "quota.sqlite3", clock=Clock())


def error(code=429, delay=None, *, daily=False, zero=False, headers=None):
    details = []
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    if daily or zero:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerDay" if daily else "GenerateRequestsPerMinute",
            "quotaValue": "0" if zero else "20",
        }]})
    cls = ClientError if code == 429 else ServerError
    return cls(code, {"error": {"details": details}}, response=SimpleNamespace(headers=headers or {}))


async def test_spacing_and_counts_survive_new_instance(guard):
    await guard.reserve("flash", 100)
    other = QuotaGovernor(guard.path, clock=guard.clock)
    with pytest.raises(QuotaDeferredError) as raised:
        await other.reserve("flash", 100)
    assert raised.value.retry_after_seconds == 16
    guard.clock.now += 16
    await other.reserve("flash", 100)


async def test_token_budget_and_no_reservation_refunds(guard):
    ticket = await guard.reserve("flash", 1000)
    await guard.success("flash", ticket, 15000)
    guard.clock.now += 16
    with pytest.raises(QuotaDeferredError, match="input_token_budget") as raised:
        await guard.reserve("flash", 2000)
    assert raised.value.retry_after_seconds == 45
    guard.clock.now += 45
    await guard.reserve("flash", 2000)


async def test_actual_tokens_cannot_refund_allowance(guard):
    ticket = await guard.reserve("flash", 15000)
    await guard.success("flash", ticket, 1)
    guard.clock.now += 16
    with pytest.raises(QuotaDeferredError, match="input_token_budget"):
        await guard.reserve("flash", 2000)


async def test_daily_budget_and_rolling_reset(tmp_path):
    clock = Clock()
    governor = QuotaGovernor(tmp_path / "daily.db", QuotaLimits(rpd=2), clock=clock)
    await governor.reserve("flash", 100)
    clock.now += 70
    await governor.reserve("flash", 100)
    clock.now += 70
    with pytest.raises(QuotaDeferredError, match="daily_budget"):
        await governor.reserve("flash", 100)
    clock.now = 1000000 + DAY
    await governor.reserve("flash", 100)


async def test_background_preserves_foreground_request_slot(guard):
    for _ in range(3):
        await guard.reserve("flash", 100, background=True)
        guard.clock.now += 16
    with pytest.raises(QuotaDeferredError, match="minute_budget"):
        await guard.reserve("flash", 100, background=True)
    await guard.reserve("flash", 100, background=False)


async def test_background_reserves_input_tokens(guard):
    await guard.reserve("flash", 10000)
    guard.clock.now += 16
    with pytest.raises(QuotaDeferredError, match="input_token_budget"):
        await guard.reserve("flash", 4000, background=True)
    await guard.reserve("flash", 4000, background=False)


async def test_model_budgets_separate_but_same_model_profiles_share(guard):
    await guard.reserve("flash", 100)
    await guard.reserve("lite", 100)
    with pytest.raises(QuotaDeferredError):
        await QuotaGovernor(guard.path, clock=guard.clock).reserve("flash", 100)


async def test_429_cooldown_shared_and_success_does_not_clear_it(guard):
    ticket = await guard.reserve("flash", 100)
    deferred = await guard.failure("flash", error(delay="120.5s"))
    assert deferred.provider_status == 429
    assert deferred.retry_after_seconds == 121.5
    await guard.success("flash", ticket, 50)
    other = QuotaGovernor(guard.path, clock=guard.clock)
    with pytest.raises(QuotaDeferredError) as raised:
        await other.reserve("flash", 100)
    assert raised.value.reason == "provider_429_cooldown"
    assert raised.value.retry_after_seconds == 121.5
    guard.clock.now += 121.5
    await other.reserve("flash", 100)


async def test_repeated_429_has_bounded_exponential_backoff(guard):
    waits = []
    for _ in range(8):
        deferred = await guard.failure("flash", error())
        waits.append(deferred.retry_after_seconds)
        guard.clock.now += deferred.retry_after_seconds
    assert waits == [60, 120, 240, 480, 900, 900, 900, 900]


@pytest.mark.parametrize("options", [{"daily": True}, {"zero": True}])
async def test_daily_or_zero_quota_opens_day_circuit(guard, options):
    deferred = await guard.failure("flash", error(delay="1s", **options))
    assert deferred.retry_after_seconds >= DAY
    guard.clock.now += 3600
    with pytest.raises(QuotaDeferredError, match="provider_daily_or_zero_quota"):
        await guard.reserve("flash", 100)


async def test_503_cannot_shorten_existing_429_cooldown(guard):
    await guard.failure("flash", error(delay="300s"))
    guard.clock.now += 10
    result = await guard.failure("flash", error(503))
    assert result.retry_after_seconds == 291
    assert result.reason == "provider_429_cooldown"


@pytest.mark.parametrize("retry, expected", [("10.5", 10.5), ("NaN", 0), ("bad", 0)])
def test_retry_after_seconds_and_malformed_headers(retry, expected):
    assert provider_cooldown(error(headers={"Retry-After": retry}), 1000)[0] == expected


def test_retry_after_date_and_structured_duration():
    assert provider_cooldown(error(headers={"retry-after": formatdate(1200, usegmt=True)}), 1000)[0] == 200
    assert provider_cooldown(error(delay={"seconds": "120", "nanos": 500000000}), 1000)[0] == 120.5


async def test_storage_error_fails_closed(tmp_path):
    governor = QuotaGovernor(tmp_path)  # A directory cannot be a database.
    with pytest.raises(QuotaDeferredError, match="quota_store_unavailable"):
        await governor.reserve("flash", 1)


async def test_clock_rollback_does_not_reset_budget(guard):
    await guard.reserve("flash", 100)
    guard.clock.now -= 100
    with pytest.raises(QuotaDeferredError) as raised:
        await guard.reserve("flash", 100)
    assert raised.value.retry_after_seconds >= 100


def test_thread_race_admits_only_one_request(guard):
    def attempt(_):
        try:
            guard._reserve("flash", 100, False)
            return True
        except QuotaDeferredError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1


def test_separate_processes_share_persistent_admission(tmp_path):
    script = """
import asyncio, sys
from gemini_quota import QuotaGovernor, QuotaDeferredError
async def main():
    try:
        await QuotaGovernor(sys.argv[1], clock=lambda: 1000000).reserve('flash', 100)
        print('allowed')
    except QuotaDeferredError:
        print('deferred')
asyncio.run(main())
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "shared.db")],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True) for _ in range(3)]
    try:
        outputs = [process.communicate(timeout=10)[0].strip() for process in processes]
        assert outputs.count("allowed") == 1
        assert outputs.count("deferred") == 2
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def sdk_client(response=None, failure=None):
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=AsyncMock(side_effect=failure, return_value=response))))


async def test_real_model_guard_prevents_second_provider_call(guard):
    response = SimpleNamespace(text=json.dumps({"summary": "Scene", "objects": [],
                                "possible_hazards": [], "uncertainties": []}))
    client = sdk_client(response)
    vision = GeminiVision(client=client, quota=guard)
    png = base64.b64encode(b"unchanged PNG bytes, no dimension checks").decode()
    await vision.summarize(png)
    with pytest.raises(QuotaDeferredError):
        await vision.summarize(png)
    client.aio.models.generate_content.assert_awaited_once()


async def test_provider_429_converts_to_safe_deferral_and_no_retry(guard):
    client = sdk_client(failure=error(delay="100s"))
    vision = GeminiVision(client=client, quota=guard)
    png = base64.b64encode(b"PNG").decode()
    for _ in range(2):
        with pytest.raises(QuotaDeferredError, match="provider_429_cooldown"):
            await vision.summarize(png)
    client.aio.models.generate_content.assert_awaited_once()


async def test_cancelled_call_keeps_request_reservation(guard):
    started = asyncio.Event()
    async def blocked(**kwargs):
        started.set()
        await asyncio.Event().wait()
    client = sdk_client(failure=blocked)
    vision = GeminiVision(client=client, quota=guard)
    png = base64.b64encode(b"PNG").decode()
    task = asyncio.create_task(vision.summarize(png))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(QuotaDeferredError):
        await vision.summarize(png)
    client.aio.models.generate_content.assert_awaited_once()


async def test_lane_reaches_central_model_limiter(guard):
    quota = SimpleNamespace(limits=QuotaLimits(), reserve=AsyncMock(side_effect=QuotaDeferredError("test")))
    vision = GeminiVision(client=sdk_client(), quota=quota)
    token = request_lane.set("background")
    try:
        with pytest.raises(QuotaDeferredError):
            await vision.summarize(base64.b64encode(b"PNG").decode(), hints={"objects": []})
    finally:
        request_lane.reset(token)
    assert quota.reserve.call_args.kwargs["background"] is True
    assert quota.reserve.call_args.args[1] > 4096


@pytest.mark.parametrize("kwargs", [{"rpm": 0}, {"rpm": -1}, {"rpd": 0},
                                  {"input_tpm": 0}, {"input_reservation": 0},
                                  {"foreground_reserve": 5}, {"rpm": True}])
def test_invalid_budget_fails_at_configuration(kwargs):
    with pytest.raises(ValueError):
        QuotaLimits(**kwargs)


def test_environment_configuration(offline_quota_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_QUOTA_DB", str(tmp_path / "configured.db"))
    monkeypatch.setenv("GEMINI_QUOTA_SCOPE", "one-project")
    monkeypatch.setenv("GEMINI_RPM_BUDGET", "3")
    governor = offline_quota_factory()
    assert governor.path == tmp_path / "configured.db"
    assert governor.limits.rpm == 3
    assert governor.scope == "one-project"
    monkeypatch.setenv("GEMINI_RPM_BUDGET", "unlimited")
    with pytest.raises(ValueError):
        offline_quota_factory()
