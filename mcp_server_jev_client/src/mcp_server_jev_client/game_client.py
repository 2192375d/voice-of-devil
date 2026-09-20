"""Godot HTTP transport: observe-only facade plus a narrow command executor."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import Degrees, RotationArguments

CONTROL_COMMANDS = ("clear_queue", "stop")
EXECUTABLE_COMMANDS = ("walk_forward", "rotate", "stop", "grab_item", "drop_item")
DEFAULT_GAME_URL = "http://127.0.0.1:3000/api/v1/commands"
GAME_TIMEOUT_SECONDS = 15.0


class GameState(BaseModel):
    """Require the state Jev uses, preserving future Godot fields verbatim."""

    model_config = ConfigDict(extra="allow", strict=True)
    status: str
    observation_sequence: int = Field(ge=1)
    simulation_time: float
    position: dict[str, float]
    rotation_degrees: dict[str, float]
    velocity: dict[str, float]
    grounded: bool
    held_item: dict[str, Any] | None
    camera: dict[str, Any]
    active_instructions: list[dict[str, Any]]
    pending_action_count: int
    last_finished_instruction: dict[str, Any] | None


@dataclass(frozen=True)
class GameFrame:
    png: bytes
    state: dict[str, Any]


@dataclass(frozen=True)
class GameAck:
    ok: bool
    status: str
    message: str | None
    request_id: str | None
    http_status: int
    cleared_count: int | None = None


class GameClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        timed_out: bool = False,
        uncertain: bool = False,
        status: str | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.timed_out = timed_out
        self.uncertain = uncertain
        self.status = status
        self.http_status = http_status


def result_status(envelope: Any) -> str | None:
    if not isinstance(envelope, dict):
        return None
    result = envelope.get("result")
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    return status if isinstance(status, str) else None


def check_http_status(http_status: int, envelope: Any, *, uncertain: bool = False) -> None:
    if not 200 <= http_status < 300:
        raise GameClientError(
            "Godot returned an unsuccessful HTTP status",
            timed_out=http_status in {408, 504},
            uncertain=uncertain,
            status=result_status(envelope),
            http_status=http_status,
        )


def parse_observation(http_status: int, envelope: Any) -> GameFrame:
    check_http_status(http_status, envelope)
    if not isinstance(envelope, dict):
        raise GameClientError("Godot returned a malformed observation envelope")
    status = result_status(envelope)
    timed_out = status == "expired" or http_status == 408
    if envelope.get("ok") is not True:
        raise GameClientError(
            "Godot observation was not successful",
            timed_out=timed_out,
            status=status,
            http_status=http_status,
        )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise GameClientError("Godot observation is missing its state")
    try:
        GameState.model_validate(result)
    except Exception as error:
        raise GameClientError("Godot observation state is invalid") from error
    if result.get("status") != "observed":
        raise GameClientError(
            "Godot did not return an observed state",
            timed_out=result.get("status") == "expired",
            status=status,
            http_status=http_status,
        )
    image = envelope.get("image")
    if not isinstance(image, dict) or image.get("mime_type") != "image/png":
        raise GameClientError("Godot observation is missing its PNG")
    data = image.get("data")
    if not isinstance(data, str) or not data:
        raise GameClientError("Godot observation contains an empty image")
    try:
        png = base64.b64decode(data, validate=True)
    except binascii.Error as error:
        raise GameClientError("Godot observation image is not valid base64") from error
    if not png:
        raise GameClientError("Godot observation contains an empty image")
    return GameFrame(png=png, state=result)


def parse_ack(http_status: int, envelope: Any) -> GameAck:
    check_http_status(http_status, envelope, uncertain=True)
    if not isinstance(envelope, dict):
        raise GameClientError("Godot returned a malformed command envelope", http_status=http_status)
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    status = result.get("status") if isinstance(result.get("status"), str) else "invalid_response"
    message = result.get("message") if isinstance(result.get("message"), str) else None
    request_id = envelope.get("request_id") if isinstance(envelope.get("request_id"), str) else None
    cleared = result.get("cleared_count") if isinstance(result.get("cleared_count"), int) else None
    timed_out = status == "expired" or http_status == 408
    ok = envelope.get("ok") is True
    if timed_out and not ok:
        raise GameClientError(
            "Godot command expired",
            timed_out=True,
            status=status,
            http_status=http_status,
        )
    return GameAck(
        ok=ok,
        status=status,
        message=message,
        request_id=request_id,
        http_status=http_status,
        cleared_count=cleared,
    )


def rotate_arguments(yaw: int) -> dict[str, Any]:
    return RotationArguments(degrees=Degrees(y=yaw)).model_dump()


class GameClient:
    """HTTP client for Godot's command API. Observation and execution share the client."""

    def __init__(self, url: str = DEFAULT_GAME_URL, *, timeout: float = GAME_TIMEOUT_SECONDS):
        self.url = url
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def observe(self) -> GameFrame:
        http_status, envelope = await self._post("observe")
        return parse_observation(http_status, envelope)

    async def execute(self, command: str, arguments: dict[str, Any] | None = None) -> GameAck:
        if command not in EXECUTABLE_COMMANDS and command not in CONTROL_COMMANDS:
            raise GameClientError(f"unsupported game command: {command}")
        http_status, envelope = await self._post(command, arguments)
        return parse_ack(http_status, envelope)

    async def _post(
        self, command: str, arguments: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        body: dict[str, Any] = {"command": command}
        if arguments:
            body["arguments"] = arguments
        try:
            response = await self._http.post(self.url, json=body)
        except httpx.TimeoutException as error:
            raise GameClientError(
                "Godot request timed out",
                timed_out=True,
                uncertain=command != "observe",
            ) from error
        except httpx.HTTPError as error:
            raise GameClientError(
                "Godot transport failed",
                uncertain=command != "observe",
            ) from error
        try:
            envelope = response.json()
        except ValueError as error:
            raise GameClientError(
                "Godot returned non-JSON",
                timed_out=response.status_code in {408, 504},
                uncertain=command != "observe",
                http_status=response.status_code,
            ) from error
        return response.status_code, envelope


class GodotObserver:
    """Observe-only adapter. Gameplay commands belong on GameClient.execute()."""

    def __init__(self, client: GameClient):
        self.client = client

    async def observe(self) -> GameFrame:
        return await self.client.observe()
