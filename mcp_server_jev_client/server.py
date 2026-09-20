import asyncio
import jev_interface
import mcp_server

MAX_OBSERVE_DEPTH = 5

async def agent_send_execute_loop(interface, user_prompt="", world_prompt=""):
    for _ in range(MAX_OBSERVE_DEPTH):
        res = await interface.send_req(user_prompt, world_prompt)
        print("=========")
        print(interface.context)

        await mcp_server.rotate(
            int(res["x_dir"][0]), int(res["y_dir"][0]), int(res["z_dir"][0])
        )
        if res["movement_actions"][1] > 0.5:
            await mcp_server.walk_forwards()

        if res["observe_action"] > 0.5:
            world_prompt = await mcp_server.observe()
            user_prompt = ""
        else:
            break

async def main():
    interface = jev_interface.JevInterface() 
    print("SERVER LOOP STARTED")
    while True:
        await agent_send_execute_loop(interface)
        await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
