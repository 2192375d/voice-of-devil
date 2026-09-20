import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_input import (BASELINE_VISION_PROMPT, VISION_PROMPT, GeminiVision,
                          Observation, PROFILES, VisionResponseError)


PNG = b"\x89PNG\r\n\x1a\noriginal bytes, without image dimensions"
ENCODED = base64.b64encode(PNG).decode()
DESCRIPTION = {"summary": "A green object is ahead.", "objects": [
    {"label": "green object", "screen_region": "center"}],
    "possible_hazards": [], "uncertainties": []}


def make_client(*, text=None, finish_reason="STOP"):
    response = SimpleNamespace(
        text=json.dumps(DESCRIPTION) if text is None else text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=SimpleNamespace(prompt_token_count=1120, candidates_token_count=45,
                                       thoughts_token_count=0, total_token_count=1165),
    )
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=AsyncMock(return_value=response)), aclose=AsyncMock()), close=Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
async def test_profiles_send_requested_settings_and_original_bytes(profile):
    client = make_client()
    vision = GeminiVision(profile, client=client)
    result = await vision.summarize_with_metrics(ENCODED)
    call = client.aio.models.generate_content.call_args.kwargs
    config = call["config"]
    assert call["model"] == PROFILES[profile].model
    assert call["contents"][0].inline_data.data == PNG
    assert call["contents"][0].inline_data.mime_type == "image/png"
    assert config.response_mime_type == "application/json"
    assert config.automatic_function_calling.disable is True
    if profile == "baseline":
        assert config.system_instruction == BASELINE_VISION_PROMPT
        assert config.thinking_config is None
        assert config.max_output_tokens is None
        assert config.media_resolution is None
        assert config.response_json_schema == Observation.model_json_schema()
    else:
        assert config.system_instruction == VISION_PROMPT
        assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
        assert config.max_output_tokens == 1024
        properties = config.response_json_schema["properties"]
        assert properties["objects"]["maxItems"] == 6
        assert properties["possible_hazards"]["maxItems"] == 2
        assert properties["uncertainties"]["maxItems"] == 2
        assert "bottom right" in config.system_instruction
        assert "approximate" in config.system_instruction
        assert "Correct their labels" in config.system_instruction
        if profile in ("low", "lite"):
            assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
        else:
            assert config.media_resolution is None
    assert result.description.model_dump() == DESCRIPTION
    assert result.metrics["gemini_ms"] >= 0
    assert result.metrics["output_tokens"] == 45
    assert result.metrics["thought_tokens"] == 0
    assert result.metrics["input_tokens"] == 1120
    assert result.metrics["finish_reason"] == "STOP"


@pytest.mark.asyncio
async def test_hints_are_canonical_and_sequence_is_not_model_input():
    client = make_client()
    vision = GeminiVision(client=client)
    await vision.summarize(ENCODED, hints={"z": 1, "a": {"d": 2, "b": 3}},
                           observation_sequence=1)
    await vision.summarize(ENCODED, hints={"a": {"b": 3, "d": 2}, "z": 1},
                           observation_sequence=99)
    first, second = client.aio.models.generate_content.call_args_list
    first_text = first.kwargs["contents"][0].text
    assert first_text == second.kwargs["contents"][0].text
    assert json.loads(first_text) == {"hints": {"z": 1, "a": {"d": 2, "b": 3}}}
    assert "observation_sequence" not in first_text


def test_cache_namespace_depends_on_all_generation_inputs(monkeypatch):
    namespaces = {name: GeminiVision(name, client=make_client()).cache_namespace
                  for name in PROFILES}
    assert len(set(namespaces.values())) == len(PROFILES)
    assert GeminiVision(client=make_client()).cache_namespace == namespaces["optimized"]
    from dataclasses import replace
    for field, value in [("prompt", "A changed prompt"), ("model", "another-model"),
                         ("max_output_tokens", 512), ("thinking_level", "low")]:
        with monkeypatch.context() as patch:
            patch.setitem(PROFILES, "optimized", replace(PROFILES["optimized"], **{field: value}))
            assert GeminiVision(client=make_client()).cache_namespace != namespaces["optimized"]
    with monkeypatch.context() as patch:
        from gemini_input import CompactObservation
        schema = CompactObservation.model_json_schema()
        schema["description"] = "Changed schema"
        patch.setattr(CompactObservation, "model_json_schema", lambda: schema)
        assert GeminiVision(client=make_client()).cache_namespace != namespaces["optimized"]


def test_default_client_disables_sdk_retries(monkeypatch):
    import gemini_input
    client_factory = Mock(return_value=make_client())
    monkeypatch.setenv("GEMINI_API_KEY", "unused-test-key")
    monkeypatch.setattr(gemini_input.genai, "Client", client_factory)
    GeminiVision()
    options = client_factory.call_args.kwargs["http_options"]
    assert options.retry_options.attempts == 1
    assert options.timeout == 60_000


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["MAX_TOKENS", "SAFETY", "OTHER", types.FinishReason.MAX_TOKENS])
async def test_non_stop_finish_reason_rejected_even_if_json_is_valid(reason):
    vision = GeminiVision(client=make_client(finish_reason=reason))
    with pytest.raises(VisionResponseError) as raised:
        await vision.summarize_with_metrics(ENCODED)
    assert raised.value.finish_reason == getattr(reason, "value", reason)
    assert raised.value.metrics["output_tokens"] == 45
    assert raised.value.metrics["gemini_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,count", [("objects", 7), ("possible_hazards", 3),
                                         ("uncertainties", 3)])
async def test_optimized_caps_validated_without_silently_truncating(field, count):
    description = dict(DESCRIPTION)
    description[field] = ([DESCRIPTION["objects"][0]] * count if field == "objects"
                          else ["Possible concern"] * count)
    client = make_client(text=json.dumps(description))
    with pytest.raises(VisionResponseError):
        await GeminiVision(client=client).summarize(ENCODED)
    # Baseline intentionally retains its old unbounded schema for comparison.
    result = await GeminiVision("baseline", client=client).summarize(ENCODED)
    assert len(getattr(result, field)) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "not json", '{"summary":"incomplete"}'])
async def test_invalid_or_empty_response_is_not_success(text):
    with pytest.raises(VisionResponseError) as raised:
        await GeminiVision(client=make_client(text=text)).summarize_with_metrics(ENCODED)
    assert raised.value.metrics["gemini_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", ["", "not base64!"])
async def test_invalid_image_encoding_does_not_call_api(encoded):
    client = make_client()
    with pytest.raises(ValueError):
        await GeminiVision(client=client).summarize(encoded)
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_metrics_are_per_response_not_shared_mutable_state():
    vision = GeminiVision(client=make_client())
    first = await vision.summarize_with_metrics(ENCODED)
    second = await vision.summarize_with_metrics(ENCODED)
    first.metrics["output_tokens"] = -1
    assert second.metrics["output_tokens"] == 45


@pytest.mark.asyncio
async def test_close_closes_both_transports_once_and_disallows_new_calls():
    client = make_client()
    vision = GeminiVision(client=client)
    await vision.aclose()
    await vision.aclose()
    client.aio.aclose.assert_awaited_once()
    client.close.assert_called_once()
    with pytest.raises(RuntimeError, match="closed"):
        await vision.summarize(ENCODED)
    client.aio.models.generate_content.assert_not_awaited()


def test_unknown_profile_rejected_before_constructing_client(monkeypatch):
    import gemini_input
    factory = Mock()
    monkeypatch.setattr(gemini_input.genai, "Client", factory)
    with pytest.raises(ValueError, match="Unknown vision profile"):
        GeminiVision("not-a-profile")
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
async def test_real_sdk_wire_serialization_offline(profile):
    """Exercise SDK converters, while a mock transport prevents network calls."""
    requests = []

    def handle(request):
        wire = json.loads(request.content)
        requests.append(wire)
        image = wire["contents"][0]["parts"][-1]["inlineData"]
        assert base64.urlsafe_b64decode(image["data"]) == PNG
        return httpx.Response(200, json={"candidates": [{
            "content": {"parts": [{"text": json.dumps(DESCRIPTION)}]}, "finishReason": "STOP",
        }]})

    client = genai.Client(api_key="unused-offline-test-key", http_options=types.HttpOptions(
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))
    vision = GeminiVision(profile, client=client)
    try:
        await vision.summarize(ENCODED)
        config = requests[0]["generationConfig"]
        if profile != "baseline":
            thinking = config["thinkingConfig"]
            # ProtoJSON accepts both SDK spellings of the underlying field.
            assert thinking.get("thinking_level", thinking.get("thinkingLevel")) == "MINIMAL"
            assert config["maxOutputTokens"] == 1024
        if profile in ("low", "lite"):
            assert config["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    finally:
        await vision.aclose()
