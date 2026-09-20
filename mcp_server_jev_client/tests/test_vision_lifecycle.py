"""MCP owns its prewarmer; voice commands use Godot state without Gemini."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_server


@pytest.mark.asyncio
async def test_lifespan_starts_one_shared_service_and_closes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-credential")
    monkeypatch.setenv("GEMINI_VISION_PROFILE", "optimized")
    monkeypatch.setenv("GEMINI_BACKGROUND_VISION", "0")
    monkeypatch.setattr(mcp_server, "vision_service", None)
    model = object()
    model_factory = Mock(return_value=model)
    client = object()
    service = SimpleNamespace(start=Mock(), observe=AsyncMock(return_value={"world": "fresh"}),
                              aclose=AsyncMock())
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(mcp_server.gemini_input, "GeminiVision", model_factory)
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", Mock(return_value=client))
    monkeypatch.setattr(mcp_server, "VisionService", service_factory)
    async with mcp_server.vision_lifespan(mcp_server.mcp):
        assert mcp_server.vision_service is service
        first, second = await asyncio.gather(mcp_server.observe(), mcp_server.observe())
        assert first == second == {"world": "fresh"}
        assert await mcp_server.start_vision_service() is service
        model_factory.assert_called_once_with(profile="optimized")
        service_factory.assert_called_once_with(model, client, mcp_server.GAME_SERVER, background_enabled=False)
        service.start.assert_called_once()
    service.aclose.assert_awaited_once()
    assert mcp_server.vision_service is None
    await mcp_server.stop_vision_service()
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_key_does_not_disable_nonvision_tools(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server, "vision_service", None)
    model_factory = Mock()
    monkeypatch.setattr(mcp_server.gemini_input, "GeminiVision", model_factory)
    async with mcp_server.vision_lifespan(mcp_server.mcp):
        assert "Hello" in await mcp_server.hello("test")
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            await mcp_server.observe()
    model_factory.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_closes_after_caller_error(monkeypatch):
    service = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(mcp_server, "vision_service", service)
    with pytest.raises(RuntimeError, match="caller failed"):
        async with mcp_server.vision_lifespan(mcp_server.mcp):
            raise RuntimeError("caller failed")
    service.aclose.assert_awaited_once()
    assert mcp_server.vision_service is None


@pytest.mark.asyncio
async def test_sdk_protocol_uses_managed_lifespan(monkeypatch):
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(mcp_server, "vision_service", None)
    start = AsyncMock(wraps=mcp_server.start_vision_service)
    stop = AsyncMock(wraps=mcp_server.stop_vision_service)
    monkeypatch.setattr(mcp_server, "start_vision_service", start)
    monkeypatch.setattr(mcp_server, "stop_vision_service", stop)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as group:
            app = mcp_server.mcp._lowlevel_server
            group.start_soon(app.run, *server_streams, app.create_initialization_options())
            async with ClientSession(*client_streams, read_timeout_seconds=3) as client:
                with anyio.fail_after(5):
                    result = await client.initialize()
                    tools = await client.list_tools()
                    assert result.server_info.name == "myserver"
                    assert "observe" in {tool.name for tool in tools.tools}
                    result = await client.call_tool("hello", {"myinput": "test"})
                    assert not result.is_error
            group.cancel_scope.cancel()
    start.assert_awaited_once()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_shutdown_finishes_cleanup_in_cancelled_scope(monkeypatch):
    import anyio

    closed = []

    async def close():
        # Real HTTP/SDK cleanup yields; an AsyncMock alone would hide cancellation.
        await anyio.sleep(0)
        closed.append(True)

    monkeypatch.setattr(mcp_server, "vision_service", SimpleNamespace(aclose=close))
    with anyio.CancelScope() as scope:
        async with mcp_server.vision_lifespan(mcp_server.mcp):
            scope.cancel()
    assert closed == [True]
    assert mcp_server.vision_service is None


@pytest.mark.asyncio
async def test_quota_deferral_is_mcp_error_not_fake_observation(monkeypatch):
    from gemini_quota import QuotaDeferredError
    from mcp.server.mcpserver.exceptions import ToolError
    monkeypatch.setattr(mcp_server, "vision_service", SimpleNamespace(
        observe=AsyncMock(side_effect=QuotaDeferredError("minute_budget", 42)),
    ))
    with pytest.raises(ToolError, match="Retry in 42s"):
        await mcp_server.mcp.call_tool("observe", {})


@pytest.mark.asyncio
async def test_voice_loop_continues_after_state_failure(monkeypatch, capsys):
    from voice_controller import GoalSupervisor

    game = SimpleNamespace(
        clear_queue=AsyncMock(return_value={"ok": True}),
        stop_walking=AsyncMock(return_value={"ok": True}),
        get_game_state=AsyncMock(side_effect=ValueError("Godot unavailable")),
    )
    decide = AsyncMock()
    supervisor = GoalSupervisor(object(), decide, game=game, reaction_delay=0)
    supervisor.start()
    token = supervisor.begin_recording()
    supervisor.submit("look ahead", token)
    await asyncio.wait_for(supervisor._mailbox.join(), timeout=1)
    await supervisor.aclose(stop_game=False)

    game.clear_queue.assert_awaited_once()
    game.get_game_state.assert_awaited_once()
    decide.assert_not_awaited()
    assert "No replacement action was sent" in capsys.readouterr().out
