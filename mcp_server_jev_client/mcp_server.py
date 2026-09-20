import httpx
from queue import Queue
import gemini_input
from gemini_input import Observation
import requests
from mcp.server.mcpserver import MCPServer
#import jev_interface
import asyncio

mcp = MCPServer("myserver")
gemini_vision = gemini_input.GeminiVision()
GAME_SERVER="127.0.0.1:3000"

requests_queue = Queue(maxsize=15)

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
async def walk_forwards() -> Observation :
	"""
	Continue walking
	"""
	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands', headers={'Content-Type':'application/json'}, json={"command":"walk_forward"})
	print(r.status_code)
	# return r.json()["resource"]
	return gemini_vision.summarize(r.json()["image"])

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

@mcp.tool()
async def observe() -> dict :
	"""
	Continue walking
	"""

	r = requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"observe"})
	image_desc = await gemini_vision.summarize(str(r.json()["image"]["data"]))
	#return r.json()
	return r.json()["result"]
	#return f"""
	### World Metrics
	#{r.json()['result']}
	### World Description
	#{image_desc}
	#"""

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

#asyncio.run(walk_forwards())

if __name__ == "__main__":
	mcp.run(transport="stdio")
