"""HTTP game envelopes, observer validation, and GODOT_API_URL migration."""

import base64
import json

import httpx
import pytest

from mcp_server_jev_client.app import resolve_godot_api_url
from mcp_server_jev_client.game_client import (
    DEFAULT_GAME_URL,
    GameClient,
    GameClientError,
    GodotObserver,
    parse_ack,
    parse_observation,
)
from test_observe import PNG, STATE


ENCODED = base64.b64encode(PNG).decode()


def envelope(ok=True, status="observed", state=None, image=..., http_status=200):
    result = {**(state or STATE), "status": status}
    body = {
        "request_id": "req-1",
        "ok": ok,
        "result": result,
        "image": {"mime_type": "image/png", "data": ENCODED} if image is ... else image,
    }
    return http_status, body


def test_resolve_godot_api_url_defaults_and_prefers_new_variable():
    assert resolve_godot_api_url({}) == DEFAULT_GAME_URL
    assert resolve_godot_api_url({
        "GODOT_API_URL": "http://127.0.0.1:3000/api/v1/commands",
        "GODOT_MCP_URL": "http://127.0.0.1:3000/mcp",
    }) == "http://127.0.0.1:3000/api/v1/commands"


def test_obsolete_mcp_url_is_a_migration_error():
    with pytest.raises(RuntimeError, match="GODOT_MCP_URL is obsolete"):
        resolve_godot_api_url({"GODOT_MCP_URL": "http://127.0.0.1:3000/mcp"})


def test_parse_observation_preserves_bytes_and_extra_state():
    frame = parse_observation(*envelope())
    assert frame.png == PNG
    assert frame.state["future_field"] == {"preserved": True}
    assert frame.state["observation_sequence"] == 42


@pytest.mark.parametrize("http_status,body", [
    envelope(ok=False, status="busy", image=None),
    envelope(status="started"),
    envelope(state={k: v for k, v in STATE.items() if k != "position"}),
    envelope(image=None),
    envelope(image={"mime_type": "image/jpeg", "data": ENCODED}),
    envelope(image={"mime_type": "image/png", "data": ""}),
    envelope(image={"mime_type": "image/png", "data": "not base64!"}),
    (200, "not-json"),
    (200, {"ok": True}),
])
def test_parse_observation_rejects_malformed_envelopes(http_status, body):
    with pytest.raises(GameClientError) as raised:
        parse_observation(http_status, body)
    assert raised.value.timed_out is False


def test_expired_observation_is_a_timeout():
    with pytest.raises(GameClientError) as raised:
        parse_observation(*envelope(ok=False, status="expired", image=None))
    assert raised.value.timed_out is True


async def test_http_client_observe_sends_only_observe_and_closes():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        _, body = envelope()
        return httpx.Response(200, json=body)

    client = GameClient("http://godot/api/v1/commands")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    observer = GodotObserver(client)
    try:
        frame = await observer.observe()
        assert frame.png == PNG
        assert frame.state["future_field"] == {"preserved": True}
        assert calls == [{"command": "observe"}]
    finally:
        await client.aclose()
    assert client._http.is_closed


async def test_http_timeout_is_classified():
    def handler(request):
        raise httpx.ReadTimeout("secret", request=request)

    client = GameClient("http://godot/api/v1/commands")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GameClientError) as raised:
            await client.observe()
        assert raised.value.timed_out is True
        assert raised.value.uncertain is False
    finally:
        await client.aclose()


@pytest.mark.parametrize("http_status", [302, 400, 408, 500, 504])
@pytest.mark.parametrize("kind", ["observation", "action"])
def test_failed_http_status_overrides_success_body(http_status, kind):
    _, body = envelope()
    if kind == "action":
        body["result"] = {"status": "started"}
    parser = parse_observation if kind == "observation" else parse_ack
    with pytest.raises(GameClientError) as raised:
        parser(http_status, body)
    assert raised.value.http_status == http_status
    assert raised.value.timed_out is (http_status in {408, 504})
    assert raised.value.uncertain is (kind == "action")


@pytest.mark.parametrize("command", ["observe", "rotate"])
async def test_http_gateway_timeout_without_json_preserves_timeout(command):
    client = GameClient("http://godot/api/v1/commands")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(504, text="Gateway timeout"),
    ))
    try:
        with pytest.raises(GameClientError) as raised:
            if command == "observe":
                await client.observe()
            else:
                await client.execute("rotate", {"degrees": {"x": 0, "y": 90, "z": 0}})
        assert raised.value.timed_out
        assert raised.value.uncertain is (command != "observe")
    finally:
        await client.aclose()
