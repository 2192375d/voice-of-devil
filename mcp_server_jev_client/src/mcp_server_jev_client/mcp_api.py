"""Python-facing Streamable HTTP MCP tools over the shared command service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .command_queue import CommandService
from .models import CommandContext, StopGameResult, VoiceCommandReceipt, VoiceCommandStatus

NonEmpty = Annotated[str, Field(min_length=1)]


def create_mcp_server(get_commands: Callable[[], CommandService]) -> MCPServer:
    mcp = MCPServer(
        "vod-voice-commands",
        instructions=(
            "Submit spoken game commands for queued scoring and bounded execution. "
            "Godot is not an MCP server; these tools talk to the Python service only."
        ),
    )

    @mcp.tool()
    async def get_command_context() -> CommandContext:
        """Read the instance and stop generation required for a new submission."""
        return await get_commands().context()

    @mcp.tool()
    async def submit_voice_command(
        command_id: UUID, transcript: NonEmpty,
        service_instance_id: UUID, generation: Annotated[int, Field(ge=0)],
    ) -> VoiceCommandReceipt:
        """Admit one voice command job. Returns immediately with a receipt, not a scored result."""
        return await get_commands().submit(
            command_id, transcript,
            context=CommandContext(service_instance_id=service_instance_id, generation=generation),
        )

    @mcp.tool()
    async def get_voice_command(command_id: UUID) -> VoiceCommandStatus:
        """Fetch job status, scores, and execution outcome by client-generated ID."""
        return await get_commands().get(command_id)

    @mcp.tool()
    async def cancel_voice_command(command_id: UUID) -> VoiceCommandStatus:
        """Cancel a queued or scoring job. Already dispatched actions are not undone."""
        return await get_commands().cancel(command_id)

    @mcp.tool()
    async def stop_game() -> StopGameResult:
        """Invalidate pending work, clear Godot's action inbox, then stop movement."""
        return await get_commands().stop_game()

    return mcp


LOCAL_MCP_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
    allowed_origins=["http://127.0.0.1", "http://127.0.0.1:*", "http://localhost", "http://localhost:*", "http://[::1]", "http://[::1]:*"],
)
