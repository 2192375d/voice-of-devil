"""Fail-closed, cross-process admission control for this application's Gemini use.

This enforces local budgets, not Google's entire project quota. All cooperating
processes must share the database, scope and settings. No keys/images are stored.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import math
import os
from pathlib import Path
import sqlite3
from time import time

request_lane = ContextVar("gemini_request_lane", default="foreground")
MINUTE = 61.0  # A little headroom around the provider's minute boundary.
DAY = 86401.0  # Conservative rolling day, rather than a reset-time burst.


class QuotaDeferredError(RuntimeError):
    def __init__(self, reason, retry_after_seconds=None, *, provider_status=None):
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.provider_status = provider_status
        suffix = (f" Retry in {math.ceil(retry_after_seconds)}s."
                  if retry_after_seconds is not None else " Check quota configuration.")
        super().__init__(f"Gemini request deferred locally ({reason}).{suffix}")


@dataclass(frozen=True)
class QuotaLimits:
    rpm: int = 4
    input_tpm: int = 16000
    rpd: int = 20
    input_reservation: int = 4096
    foreground_reserve: int = 1

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in
               (self.rpm, self.input_tpm, self.rpd, self.input_reservation)):
            raise ValueError("Gemini quota budgets must be positive integers")
        if (type(self.foreground_reserve) is not int
                or not 0 <= self.foreground_reserve <= min(self.rpm, self.rpd)):
            raise ValueError("Invalid Gemini foreground reserve")


def _positive_delay(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def provider_cooldown(error, now):
    """Read structured Google RetryInfo / Retry-After, never parse/log credentials."""
    delays = []
    daily_or_zero = False
    body = getattr(error, "details", {})
    if isinstance(body, dict):
        body = body.get("error", body)
    details = body.get("details", []) if isinstance(body, dict) else []
    if not isinstance(details, list):
        details = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("@type", "")).endswith("RetryInfo"):
            delay = detail.get("retryDelay", detail.get("retry_delay"))
            if isinstance(delay, str) and delay.endswith("s"):
                delay = _positive_delay(delay[:-1])
            elif isinstance(delay, dict):
                seconds = _positive_delay(delay.get("seconds", 0))
                nanos = _positive_delay(delay.get("nanos", 0))
                delay = seconds + nanos / 1e9 if seconds is not None and nanos is not None else None
            else:
                delay = None
            if delay is not None:
                delays.append(delay)
        if str(detail.get("@type", "")).endswith("QuotaFailure"):
            violations = detail.get("violations", [])
            if not isinstance(violations, list):
                continue
            for violation in violations:
                if not isinstance(violation, dict):
                    continue
                quota_id = str(violation.get("quotaId", "")).lower()
                daily_or_zero |= ("perday" in quota_id or "per_day" in quota_id
                                  or str(violation.get("quotaValue", "")) == "0")
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After", headers.get("retry-after"))
    if retry_after is not None:
        seconds = _positive_delay(retry_after)
        if seconds is None:
            try:
                seconds = max(0, parsedate_to_datetime(retry_after).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
        if seconds is not None:
            delays.append(seconds)
    return max(delays, default=0), daily_or_zero


class QuotaGovernor:
    def __init__(self, path, limits=None, *, scope="default", clock=time):
        self.path = Path(path)
        self.limits = limits or QuotaLimits()
        self.scope = scope
        self.clock = clock
        if not scope.strip():
            raise ValueError("GEMINI_QUOTA_SCOPE cannot be empty")

    @classmethod
    def from_env(cls):
        limits = QuotaLimits(
            rpm=int(os.environ.get("GEMINI_RPM_BUDGET", "4")),
            input_tpm=int(os.environ.get("GEMINI_INPUT_TPM_BUDGET", "16000")),
            rpd=int(os.environ.get("GEMINI_RPD_BUDGET", "20")),
            input_reservation=int(os.environ.get("GEMINI_INPUT_TOKEN_RESERVATION", "4096")),
            foreground_reserve=int(os.environ.get("GEMINI_FOREGROUND_RESERVE", "1")),
        )
        return cls(os.environ.get("GEMINI_QUOTA_DB", str(
            Path(__file__).resolve().parent / ".state" / "gemini-quota.sqlite3")),
            limits, scope=os.environ.get("GEMINI_QUOTA_SCOPE", "default"))

    def _transaction(self, operation):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=0.2)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, model TEXT NOT NULL,
                started REAL NOT NULL, tokens INTEGER NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS request_window ON requests(scope, model, started)")
            connection.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
                scope TEXT NOT NULL, model TEXT NOT NULL, until REAL NOT NULL,
                streak INTEGER NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(scope, model))""")
            result = operation(connection)
            connection.commit()
            return result
        except (sqlite3.Error, OSError) as error:
            # A missing/corrupt/locked database must never turn the limiter off.
            raise QuotaDeferredError("quota_store_unavailable", 1) from error
        finally:
            if connection is not None:
                connection.close()

    async def reserve(self, model, estimated_tokens, *, background=False):
        return await asyncio.to_thread(self._reserve, model, estimated_tokens, background)

    def _reserve(self, model, estimated_tokens, background):
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("Input-token reservation must be a positive integer")

        def operation(db):
            now = self.clock()
            state = db.execute("SELECT until, reason FROM cooldowns WHERE scope=? AND model=?",
                               (self.scope, model)).fetchone()
            if state and state[0] > now:
                raise QuotaDeferredError(state[1], state[0] - now)
            db.execute("DELETE FROM requests WHERE started < ?", (now - DAY,))
            rows = db.execute("SELECT started, tokens FROM requests WHERE scope=? AND model=? ORDER BY started",
                              (self.scope, model)).fetchall()
            minute = [row for row in rows if row[0] > now - MINUTE]
            reserve = self.limits.foreground_reserve if background else 0
            rpm = self.limits.rpm - reserve
            rpd = self.limits.rpd - reserve
            tpm = self.limits.input_tpm - reserve * self.limits.input_reservation
            if rpm <= 0 or rpd <= 0 or estimated_tokens > tpm:
                raise QuotaDeferredError("background_reserve" if background else "input_budget_too_small")
            waits = []
            if rows:
                spacing = 60.0 / self.limits.rpm + 1.0
                waits.append((rows[-1][0] + spacing - now, "request_spacing"))
            if len(rows) >= rpd:
                waits.append((rows[-rpd][0] + DAY - now, "daily_budget"))
            if len(minute) >= rpm:
                waits.append((minute[-rpm][0] + MINUTE - now, "minute_budget"))
            used = sum(row[1] for row in minute)
            for timestamp, tokens in minute:
                if used + estimated_tokens <= tpm:
                    break
                waits.append((timestamp + MINUTE - now, "input_token_budget"))
                used -= tokens
            delay, reason = max(waits, default=(0, "ready"))
            if delay > 0:
                raise QuotaDeferredError(reason, delay)
            cursor = db.execute("INSERT INTO requests(scope, model, started, tokens) VALUES(?,?,?,?)",
                                (self.scope, model, now, estimated_tokens))
            return cursor.lastrowid

        return self._transaction(operation)

    async def success(self, model, reservation_id, actual_input_tokens=None):
        def operation(db):
            # Never refund a reservation. Unexpected extra tokens restrict later
            # calls; estimates cannot guarantee provider-side token accounting.
            if type(actual_input_tokens) is int and actual_input_tokens > 0:
                db.execute("UPDATE requests SET tokens=MAX(tokens, ?) WHERE id=? AND scope=? AND model=?",
                           (actual_input_tokens, reservation_id, self.scope, model))
            # An older successful in-flight call must NOT clear a newer cooldown.
            db.execute("UPDATE cooldowns SET streak=0 WHERE scope=? AND model=? AND until<=?",
                       (self.scope, model, self.clock()))
        await asyncio.to_thread(self._transaction, operation)

    async def failure(self, model, error):
        status = getattr(error, "code", None)
        if status not in (429, 503):
            return None

        def operation(db):
            now = self.clock()
            previous = db.execute("SELECT until, streak, reason FROM cooldowns WHERE scope=? AND model=?",
                                  (self.scope, model)).fetchone()
            streak = min((previous[1] if previous else 0) + 1, 10)
            retry, daily = provider_cooldown(error, now)
            base = 60 if status == 429 else 5
            delay = max(retry + 1, min(base * 2 ** (streak - 1), 900), DAY if daily else 0)
            reason = "provider_daily_or_zero_quota" if daily else f"provider_{status}_cooldown"
            until = now + delay
            if previous and previous[0] > until:
                until, reason = previous[0], previous[2]
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?,?,?,?)",
                       (self.scope, model, until, streak, reason))
            return QuotaDeferredError(reason, until - now, provider_status=status)

        return await asyncio.to_thread(self._transaction, operation)
