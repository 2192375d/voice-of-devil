import httpx
import anyio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
import gemini_input
from vision_service import VisionService
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from gemini_quota import QuotaDeferredError
from observation import fetch_frame
#import jev_interface
import asyncio

GAME_SERVER="127.0.0.1:3000"
vision_service = None
logger = logging.getLogger(__name__)


async def start_vision_service(*, required=False):
	"""Manage the optional Gemini service for explicit vision callers."""
	global vision_service
	if vision_service is None:
		if not os.environ.get("GEMINI_API_KEY"):
			if required:
				raise ValueError("Set GEMINI_API_KEY to enable observations")
			logger.warning("Vision prewarming disabled: GEMINI_API_KEY is not configured")
			return None
		model = gemini_input.GeminiVision(profile=os.environ.get("GEMINI_VISION_PROFILE", "optimized"))
		vision_service = VisionService(model, httpx.AsyncClient(timeout=15.0), GAME_SERVER,
			background_enabled=os.environ.get("GEMINI_BACKGROUND_VISION", "0") == "1")
		vision_service.start()
	return vision_service


async def stop_vision_service():
	global vision_service
	service, vision_service = vision_service, None
	if service is not None:
		await service.aclose()


@asynccontextmanager
async def vision_lifespan(server):
	try:
		await start_vision_service()
		yield
	finally:
		# MCP uses AnyIO cancellation scopes; cleanup must finish even when the
		# serving task group has already been cancelled.
		with anyio.CancelScope(shield=True):
			await stop_vision_service()


mcp = MCPServer("myserver", lifespan=vision_lifespan)

@mcp.tool()
async def hello(myinput : str) -> str :
	"""
	Get your name with age
	"""
	# logic
	return f"""
	Hello
	"""

@mcp.tool()
async def walk_forwards(meters: float) -> dict :
	"""
	Move the requested number of meters using the game timer; negative moves backward, zero does nothing.
	"""
	return await send_game_command("walk_forward", {"meters": meters})

@mcp.tool()
async def stop_walking() -> dict :
	"""
	Stop walking and rotating.
	"""

	return await send_game_command("stop")

@mcp.tool()
async def clear_queue() :
	"""
	Continue walking
	"""

	await stop_walking()
	return "DONE" 

async def observe() -> dict[str, Any]:
	"""Return fresh game state and Gemini's exact-frame interpretation."""
	service = await start_vision_service(required=True)
	return await service.observe()


@mcp.tool()
async def get_game_state() -> dict[str, Any]:
	"""Fetch authoritative Godot state without calling Gemini or returning image bytes."""
	async with httpx.AsyncClient(timeout=15.0) as client:
		frame = await fetch_frame(client, GAME_SERVER)
	state = frame["result"]
	return {
		"observation_sequence": state["observation_sequence"],
		"simulation_time": state["simulation_time"],
		"game_state": {key: value for key, value in state.items()
			if key not in {"status", "message", "observation_sequence", "simulation_time"}},
		"vision": None,
	}


async def send_game_command(command: str, arguments=None) -> dict:
	"""Bound the request and surface both HTTP errors and Godot rejections. No retries."""
	async with httpx.AsyncClient(timeout=15.0) as client:
		response = await client.post(f"http://{GAME_SERVER}/api/v1/commands",
			json={"command": command, "arguments": arguments or {}})
	response.raise_for_status()
	result = response.json()
	if not isinstance(result, dict) or result.get("ok") is not True:
		raise ValueError(f"Godot rejected {command}: {result}")
	logger.info("Godot command=%s result=%s", command, result.get("result"))
	return result


@mcp.tool(name="observe", structured_output=True)
async def observe_tool() -> dict[str, Any]:
	"""Return fresh game state and Gemini's exact-frame interpretation, or a quota deferral."""
	try:
		return await observe()
	except QuotaDeferredError as error:
		# Mark this as an anticipated MCP tool failure so its safe retry message
		# reaches the client. Direct callers still receive the typed error.
		raise ToolError(str(error)) from None

@mcp.tool()
async def grab_item() -> dict :
	"""
	Pick up a nearby item while idle.
	"""

	return await send_game_command("grab_item")

@mcp.tool()
async def drop_item() -> dict :
	"""
	Drop the held item while idle.
	"""

	return await send_game_command("drop_item")


@mcp.tool()
async def interact() -> dict:
	"""Interact with a nearby door while idle."""
	return await send_game_command("interact")

@mcp.tool()
async def rotate(x:int=0, y:int=90, z:int=0) -> dict :
	"""
	Turn by relative yaw degrees: positive right, negative left; x and z must be zero.
	"""

	return await send_game_command("rotate", {"degrees": {"x": x, "y": y, "z": z}})

#asyncio.run(walk_forwards(5))

if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	mcp.run(transport="stdio")
