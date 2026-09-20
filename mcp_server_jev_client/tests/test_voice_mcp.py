"""The CLI's actual MCP client, including wire parsing and reordered requests."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from mcp_server_jev_client.command_queue import CommandService
from mcp_server_jev_client.executor import GameExecutor
from mcp_server_jev_client.mcp_api import LOCAL_MCP_SECURITY, create_mcp_server
from mcp_server_jev_client.models import VoiceCommandReceipt
from mcp_server_jev_client.voice import McpCommandClient, VoiceController
from test_executor import ScriptedGame
from test_voice import FakeCapture, FakeRecognizer, spoken


@asynccontextmanager
async def wire_client():
    game = ScriptedGame()
    service = CommandService(
        observer=game, vision=None, jev=None,
        executor=GameExecutor(game, game), pipeline_lock=asyncio.Lock(),
    )
    # Admission/status/control do not need a model worker, audio or a real game.
    mcp = create_mcp_server(lambda: service)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=LOCAL_MCP_SECURITY)
    async with mcp.session_manager.run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
        ) as http:
            async with streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    client = McpCommandClient("http://127.0.0.1:8000/mcp")
                    client._session = session
                    yield client, service, game


async def test_cli_parses_receipt_status_and_stop_over_real_mcp():
    async with wire_client() as bundle:
        client, service, game = bundle
        command_id = uuid4()
        receipt = await client.submit(command_id, "walk forward")
        assert receipt.accepted and receipt.command_id == command_id
        assert isinstance(receipt.service_instance_id, UUID)
        status = await client.get(command_id)
        assert status.found and status.status == "queued"
        assert status.service_instance_id == receipt.service_instance_id
        stop = await client.stop_game()
        assert stop.game_stopped and stop.cancelled_jobs == 1
        assert stop.service_instance_id == receipt.service_instance_id
        assert (await client.get(command_id)).status == "cancelled"
        assert game.commands == ["clear_queue", "stop"]


@pytest.mark.parametrize("delayed_tool", ["get_command_context", "submit_voice_command"])
async def test_f9_fences_submission_arriving_after_stop(monkeypatch, delayed_tool):
    async with wire_client() as bundle:
        client, service, game = bundle
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = client._session.call_tool

        async def delayed_call(name, arguments=None, **kwargs):
            if name == delayed_tool:
                entered.set()
                await release.wait()
            return await original_call(name, arguments, **kwargs)

        monkeypatch.setattr(client._session, "call_tool", delayed_call)
        controller = VoiceController(FakeCapture(), FakeRecognizer(text="walk forward"), client)
        task = asyncio.create_task(controller._transcribe_and_submit(spoken()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(controller._priority_stop(), 2)
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
        assert not service._jobs
        assert game.commands == ["clear_queue", "stop"]
        # A fresh gesture after stopping is admitted, without replaying the old one.
        monkeypatch.setattr(client._session, "call_tool", original_call)
        assert (await client.submit(uuid4(), "turn right")).accepted


async def test_server_rejects_previous_instance_context():
    async with wire_client() as bundle:
        client, service, _ = bundle
        result = await client._session.call_tool("submit_voice_command", {
            "command_id": str(uuid4()), "transcript": "walk forward",
            "service_instance_id": str(uuid4()), "generation": service.generation,
        })
        receipt = client._parse_result(result, VoiceCommandReceipt)
        assert not receipt.accepted
        assert receipt.error == "stale_command_context"
        assert not service._jobs


async def test_server_requires_context_for_submission():
    async with wire_client() as bundle:
        client, service, _ = bundle
        result = await client._session.call_tool("submit_voice_command", {
            "command_id": str(uuid4()), "transcript": "walk forward",
        })
        assert result.is_error
        assert not service._jobs


def test_json_uuid_parsing_keeps_other_fields_strict():
    result = SimpleNamespace(is_error=False, structured_content={
        "accepted": "true", "service_instance_id": str(uuid4()), "command_id": str(uuid4()),
    })
    with pytest.raises(ValidationError):
        McpCommandClient._parse_result(result, VoiceCommandReceipt)
    result.is_error = True
    with pytest.raises(RuntimeError, match="successful structured response"):
        McpCommandClient._parse_result(result, VoiceCommandReceipt)
