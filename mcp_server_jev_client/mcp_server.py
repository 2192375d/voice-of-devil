import httpx
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("myserver")

@mcp.tool()
async def my_function(myinput : str) -> dict :
	"""
	Get your name with age
	"""
	# logic
	return f"""
	Name: {myinput},
	Age: 20
	"""

if __name__ == "__main__":
	mcp.run(transport="stdio")
