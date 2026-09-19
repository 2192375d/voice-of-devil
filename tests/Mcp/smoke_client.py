"""Interoperability check using the official MCP Python SDK (mcp==2.2.0).

Run with --fixture to start the C# test server, or --url to test a running game.
This is a test client, not the Jev gameplay loop. It moves and stops the player.
"""
import argparse
import asyncio
import os
from pathlib import Path
import socket
import subprocess

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(url: str, fixture: bool):
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            initialized = (await session.initialize()).model_dump(by_alias=True)
            assert initialized["protocolVersion"] == "2025-11-25"
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "walk_forward", "rotate", "stop", "grab_item", "drop_item", "observe", "clear_queue"
            }

            async def call(name, arguments=None):
                return (await session.call_tool(name, arguments or {})).model_dump(by_alias=True)

            await call("clear_queue")
            await call("stop")
            try:
                assert (await call("walk_forward"))["structuredContent"]["status"] == "started"
                assert (await call("walk_forward"))["structuredContent"]["status"] == "already_running"
                assert (await call("rotate", {"degrees": {"x": 0, "y": 90, "z": 0}}))["structuredContent"]["status"] == "started"
                busy = await call("grab_item")
                assert busy["isError"] and busy["structuredContent"]["status"] == "busy"
            finally:
                assert (await call("stop"))["structuredContent"]["status"] == "stopped"

            if fixture:
                assert (await call("drop_item"))["structuredContent"]["status"] == "hands_empty"
                assert (await call("grab_item"))["structuredContent"]["status"] == "no_item_in_reach"
            observation = await call("observe")
            if fixture:
                assert observation["isError"] and observation["structuredContent"]["status"] == "observation_error"
            else:
                assert not observation["isError"]
                assert any(block["type"] == "image" and block["mimeType"] == "image/png" for block in observation["content"])
                assert observation["structuredContent"]["camera"]["width"] == 512
            print("Official MCP client interoperability check passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:3000/mcp")
    args = parser.parse_args()
    process = None
    try:
        if args.fixture:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            assembly = Path(__file__).parent / "bin/Debug/net8.0/Mcp.dll"
            process = subprocess.Popen(
                ["dotnet", str(assembly), "--serve", str(port)],
                env={**os.environ, "DOTNET_ROLL_FORWARD": "Major"},
                stdout=subprocess.PIPE, text=True,
            )
            assert process.stdout.readline().strip() == "MCP fixture ready"
            args.url = f"http://127.0.0.1:{port}/mcp"
        asyncio.run(check(args.url, args.fixture))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
