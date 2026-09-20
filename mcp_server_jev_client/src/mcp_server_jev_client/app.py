"""One-shot observation API; it never dispatches gameplay actions."""

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from time import perf_counter

import httpx
import httpx2
from backboard import BackboardClient
from backboard.exceptions import BackboardAPIError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import errors, types
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import REQUEST_TIMEOUT

from .models import ObserveRequest, ObserveResponse
from .providers import GeminiVision, GodotObserver, JevDecider

logger = logging.getLogger(__name__)


def is_timeout(error: BaseException) -> bool:
    """SDKs may wrap transport timeouts; classify them without exposing messages."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
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


@asynccontextmanager
async def open_services():
    load_dotenv()
    required = ("GEMINI_API_KEY", "BACKBOARD_APIKEY")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

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
        streams = await stack.enter_async_context(
            streamable_http_client(os.getenv("GODOT_MCP_URL", "http://127.0.0.1:3000/mcp"))
        )
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await asyncio.wait_for(session.initialize(), timeout=15)
        yield Services(GodotObserver(session), GeminiVision(async_gemini), JevDecider(backboard))


def create_app(*, services_factory=open_services, timeout_seconds: float = 60) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with services_factory() as services:
            app.state.services = services
            app.state.pipeline_lock = asyncio.Lock()
            yield

    app = FastAPI(title="Godot observation service", lifespan=lifespan)

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
            timings = {}
            try:
                async with asyncio.timeout(timeout_seconds):
                    tick = perf_counter()
                    frame = await services.game.observe()
                    sequence = frame.state["observation_sequence"]
                    timings[stage] = (perf_counter() - tick) * 1000

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
