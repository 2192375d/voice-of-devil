import httpx
import logging
from typing import Any
import gemini_input
from observation import observe_world
import requests
from mcp.server.mcpserver import MCPServer
#import jev_interface
import asyncio

mcp = MCPServer("myserver")
gemini_vision = None
GAME_SERVER="127.0.0.1:3000"

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
	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands', headers={'Content-Type':'application/json'}, json={"command":"walk_forward", "arguments":{"meters":meters}})
	r.raise_for_status()
	return r.json()

@mcp.tool()
async def stop_walking() -> dict :
	"""
	Continue walking
	"""

	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"stop"})
	print(r.status_code)
	return r.json()

@mcp.tool()
async def clear_queue() :
	"""
	Continue walking
	"""

	await stop_walking()
	return "DONE" 

@mcp.tool(structured_output=True)
async def observe() -> dict[str, Any]:
	"""Return current game state and Gemini's interpretation of the hinted screenshot."""
	global gemini_vision
	if gemini_vision is None:
		gemini_vision = gemini_input.GeminiVision()
	# Close the game connection after each observation in both CLI and MCP use.
	async with httpx.AsyncClient(timeout=15.0) as client:
		return await observe_world(client, GAME_SERVER, gemini_vision)

@mcp.tool()
async def grab_item() -> dict :
	"""
	Continue walking
	"""

	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"grab_item"})
	print(r.status_code)
	return r.json()

@mcp.tool()
async def drop_item() -> dict :
	"""
	Continue walking
	"""

	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"drop_item"})
	print(r.status_code)
	return r.json()

@mcp.tool()
async def rotate(x:int=0, y:int=90, z:int=0) -> dict :
	"""
	Rotate camera to specific position
	"""

	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"rotate","arguments":{"degrees":{"x":x,"y":y,"z":z}}})
	print(r.status_code)
	return r.json()

#asyncio.run(walk_forwards(5))

if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	mcp.run(transport="stdio")
