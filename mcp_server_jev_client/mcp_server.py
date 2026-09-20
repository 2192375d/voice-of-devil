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
async def walk_forwards(meters: float) -> None :
	"""
	Move the requested number of meters using the game timer; negative moves backward, zero does nothing.
	"""
	requests_queue.put(lambda : requests.post(f'http://{GAME_SERVER}/api/v1/commands', headers={'Content-Type':'application/json'}, json={"command":"walk_forward", "arguments":{"meters":meters}}))
	#print(r.status_code)
	# return r.json()["resource"]
	#return gemini_vision.summarize(r.json()["image"])

@mcp.tool()
async def stop_walking() -> None :
	"""
	Continue walking
	"""

	requests_queue.put(lambda : requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"stop"}))
	#r = 
	#print(r.status_code)
	#return r.json()

# @mcp.tool()
# async def clear_queue() -> None:
# 	"""
# 	Continue walking
# 	"""
# 
# 	await stop_walking()
	
# @mcp.tool()
async def observe() -> str :
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
async def grab_item() -> None :
	"""
	Continue walking
	"""
	requests_queue.put(lambda : requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"grab_item"}))
	#print(r.status_code)
	#return r.json()

@mcp.tool()
async def drop_item() -> None :
	"""
	Continue walking
	"""

	requests_queue.put(lambda : requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"drop_item"}))
	#print(r.status_code)
	#return r.json()

@mcp.tool()
async def rotate(x:int=0, y:int=90, z:int=0) -> None :
	"""
	Rotate camera to specific position
	"""

	requests_queue.put(lambda : requests.post(f'http://{GAME_SERVER}/api/v1/commands',headers={'Content-Type':'application/json'}, json={"command":"rotate","arguments":{"degrees":{"x":x,"y":y,"z":z}}}))
	#print(r.status_code)
	#return r.json()

#asyncio.run(walk_forwards(5))

if __name__ == "__main__":
	mcp.run(transport="stdio")
