"""Observation API plus the Python MCP command surface. /observe never executes actions."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
import httpx2
from backboard import BackboardClient
from backboard.exceptions import BackboardAPIError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import errors, types
from mcp.types import REQUEST_TIMEOUT
from mcp import MCPError

from .command_queue import CommandService
from .executor import GameExecutor, NullExecutor
from .game_client import DEFAULT_GAME_URL, GAME_TIMEOUT_SECONDS, GameClient, GameClientError, GodotObserver
from .mcp_api import LOCAL_MCP_SECURITY, create_mcp_server
from .models import ObserveRequest, ObserveResponse
from .providers import GeminiVision, JevDecider

logger = logging.getLogger(__name__)


def resolve_godot_api_url(env: Any = None) -> str:
    source = os.environ if env is None else env
    configured = str(source.get("GODOT_API_URL", "")).strip()
    obsolete = str(source.get("GODOT_MCP_URL", "")).strip()
    if configured:
        return configured
    if obsolete:
        raise RuntimeError(
            "GODOT_MCP_URL is obsolete. Set GODOT_API_URL="
            f"{DEFAULT_GAME_URL} (Godot's HTTP command API). "
            "Do not point this service at /mcp; Godot no longer implements MCP."
        )
    return DEFAULT_GAME_URL


def is_timeout(error: BaseException) -> bool:
    """SDKs may wrap transport timeouts; classify them without exposing messages."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, GameClientError) and error.timed_out:
            return True
        if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx2.TimeoutException)):
            return True
        if isinstance(error, MCPError) and error.code == REQUEST_TIMEOUT:
            return True
        if isinstance(error, errors.APIError) and error.code == 504:
            return True
        if isinstance(error, BackboardAPIError) and error.status_code == 504:
            return True
        error = error.__cause__ or error.__context__
    return False


@dataclass
class Services:
    game: GodotObserver
    vision: GeminiVision
    jev: JevDecider
    transport: Any = None
    commands: CommandService | None = None
    mcp: Any = None
    dispatch: bool = True


async def observe_and_summarize(game: GodotObserver, vision: GeminiVision):
    tick = perf_counter()
    frame = await game.observe()
    timings = {"mcp": (perf_counter() - tick) * 1000}
    tick = perf_counter()
    observation = await vision.summarize(frame.png)
    timings["gemini"] = (perf_counter() - tick) * 1000
    return frame, observation, timings


async def run_observe_pipeline(services: Services, goal: str, timeout_seconds: float) -> ObserveResponse:
    start = perf_counter()
    async with asyncio.timeout(timeout_seconds):
        frame, observation, timings = await observe_and_summarize(services.game, services.vision)
        tick = perf_counter()
        decision = await services.jev.decide(goal, frame.state, observation)
        timings["jev"] = (perf_counter() - tick) * 1000
        timings["total"] = (perf_counter() - start) * 1000
        return ObserveResponse(
            observation_sequence=frame.state["observation_sequence"],
            game_state=frame.state,
            observation=observation,
            decision=decision,
            timings_ms=timings,
        )


@asynccontextmanager
async def open_services():
    load_dotenv()
    required = ("GEMINI_API_KEY", "BACKBOARD_APIKEY")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    url = resolve_godot_api_url()
    dispatch = os.getenv("VOICE_DISPATCH", "1").strip() not in {"0", "false", "False"}

    async with AsyncExitStack() as stack:
        gemini = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(timeout=60_000),
        )
        stack.callback(gemini.close)
        async_gemini = await stack.enter_async_context(gemini.aio)
        backboard = await stack.enter_async_context(
            BackboardClient(api_key=os.environ["BACKBOARD_APIKEY"], timeout=60)
        )
        transport = GameClient(url, timeout=GAME_TIMEOUT_SECONDS)
        stack.push_async_callback(transport.aclose)
        yield Services(
            GodotObserver(transport),
            GeminiVision(async_gemini),
            JevDecider(backboard),
            transport=transport,
            dispatch=dispatch,
        )


def create_app(*, services_factory=open_services, timeout_seconds: float = 60) -> FastAPI:
    holder: dict[str, Any] = {"commands": None}
    mcp = create_mcp_server(lambda: holder["commands"])
    mcp_asgi = mcp.streamable_http_app(streamable_http_path="/", transport_security=LOCAL_MCP_SECURITY)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with services_factory() as services:
            lock = asyncio.Lock()
            if services.commands is None:
                if services.transport is None:
                    executor = NullExecutor()
                else:
                    executor = GameExecutor(services.transport, services.game)
                    executor.bind(
                        generation=lambda: services.commands.generation,
                        job_cancelled=lambda job: (
                            job.status == "cancelled"
                            or job.generation != services.commands.generation
                        ),
                    )
                services.commands = CommandService(
                    observer=services.game,
                    vision=services.vision,
                    jev=services.jev,
                    executor=executor,
                    pipeline_lock=lock,
                    score_timeout=timeout_seconds,
                    dispatch=services.dispatch,
                    observe_and_summarize=observe_and_summarize,
                )
            holder["commands"] = services.commands
            app.state.services = services
            app.state.pipeline_lock = services.commands.pipeline_lock
            services.mcp = mcp
            async with mcp.session_manager.run():
                services.commands.start()
                try:
                    yield
                finally:
                    await services.commands.aclose()
                    holder["commands"] = None

    app = FastAPI(title="Godot observation service", lifespan=lifespan)
    app.mount("/mcp", mcp_asgi)

    @app.post("/observe", response_model=ObserveResponse)
    async def observe(request: ObserveRequest) -> ObserveResponse:
        lock = app.state.pipeline_lock
        if lock.locked():
            raise HTTPException(409, detail={"stage": "admission", "error": "observation_in_progress"})

        # No await between checking an unlocked asyncio.Lock and acquiring it.
        async with lock:
            services = app.state.services
            start = perf_counter()
            stage = "mcp"
            sequence = None
            try:
                async with asyncio.timeout(timeout_seconds):
                    tick = perf_counter()
                    frame = await services.game.observe()
                    sequence = frame.state["observation_sequence"]
                    timings = {"mcp": (perf_counter() - tick) * 1000}

                    stage = "gemini"
                    tick = perf_counter()
                    observation = await services.vision.summarize(frame.png)
                    timings[stage] = (perf_counter() - tick) * 1000

                    stage = "jev"
                    tick = perf_counter()
                    decision = await services.jev.decide(request.goal, frame.state, observation)
                    timings[stage] = (perf_counter() - tick) * 1000
                    timings["total"] = (perf_counter() - start) * 1000
                    response = ObserveResponse(
                        observation_sequence=sequence,
                        game_state=frame.state,
                        observation=observation,
                        decision=decision,
                        timings_ms=timings,
                    )
                logger.info("observation=%s timings_ms=%s", sequence, timings)
                return response
            except Exception as error:
                # Provider exceptions can contain credentials, image data, or prompts.
                timed_out = is_timeout(error)
                error_code = "upstream_timeout" if timed_out else "upstream_error"
                logger.warning("observation=%s stage=%s error=%s elapsed_ms=%.1f",
                               sequence, stage, error_code, (perf_counter() - start) * 1000)
                raise HTTPException(504 if timed_out else 502, detail={
                    "stage": stage, "error": error_code,
                }) from None

    return app


app = create_app()
