from __future__ import annotations

import base64
import json
import traceback

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.core.llm import LLMProviderError, OpenAICompatibleClient

MODEL = "verified-model"
KEY = "test-secret-never-expose"


def completion(content="complete", reason="stop", calls=None):
    return {"choices": [{"message": {"content": content, "reasoning_content": "private reasoning", "tool_calls": calls}, "finish_reason": reason}]}


def client_for(handler):
    return OpenAICompatibleClient("http://router.test/v1/", api_key=SecretStr(KEY), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_catalog_uses_authenticated_v1_once_and_deduplicates():
    def handler(request):
        assert str(request.url) == "http://router.test/v1/models"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"data": [{"id": "brain"}, {"id": MODEL}, {"id": "brain"}]})
    client = client_for(handler)
    assert await client.list_models() == ["brain", MODEL]
    assert await client.health() is True


@pytest.mark.asyncio
async def test_vision_schema_and_options_use_only_standard_fields(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_models", MODEL)
    image = base64.b64encode(b"\xff\xd8\xff\x00").decode()
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "http://router.test/v1/chat/completions"
        assert set(body) == {"model", "messages", "stream", "temperature", "max_tokens", "response_format"}
        assert body["model"] == MODEL
        assert body["messages"] == [{"role": "system", "content": "director"}, {"role": "user", "content": [
            {"type": "text", "text": "inspect"}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]}]
        assert body["max_tokens"] == 8192
        assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "director_response", "strict": True, "schema": schema}}
        return httpx.Response(200, json=completion('{"answer":"ok"}'))
    result = await client_for(handler).chat(MODEL, "inspect", system="director", images=[image], format=schema, require_vision=True, options={"temperature": 0.1, "num_predict": 8192})
    assert result == '{"answer":"ok"}'


@pytest.mark.asyncio
async def test_native_call_ids_and_result_replay_are_preserved():
    call = {"id": "call_stable_17", "type": "function", "function": {"name": "read_status", "arguments": '{"project_id":"abc"}'}}
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=completion(None, "tool_calls", [call]))
        assert body["messages"][1] == {"role": "assistant", "content": "", "tool_calls": [
            {**call, "function": {"name": "read_status", "arguments": '{"project_id": "abc"}'}}]}
        assert body["messages"][2] == {"role": "tool", "content": "ready", "tool_call_id": "call_stable_17"}
        return httpx.Response(200, json=completion())
    client = client_for(handler)
    messages = [{"role": "user", "content": "status"}]
    first = await client.chat_response(MODEL, messages=messages, tools=[{"type": "function", "function": {"name": "read_status"}}])
    assert first["tool_calls"] == [{"id": "call_stable_17", "name": "read_status", "arguments": {"project_id": "abc"}}]
    await client.chat_response(MODEL, messages=[*messages, {"role": "assistant", "content": first["content"], "tool_calls": first["tool_calls"]}, {"role": "tool", "content": "ready", "tool_call_id": first["tool_calls"][0]["id"], "tool_name": "read_status"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("data,pattern", [
    (completion("partial", "length"), "incomplete"),
    (completion("", "stop"), "empty final"),
    (completion("", "tool_calls", [{"function": {"name": "read", "arguments": "{}"}}]), "tool call ID"),
    (completion("", "tool_calls", [{"id": "a", "function": {"name": "read", "arguments": "invalid"}}]), "arguments JSON"),
    (completion("", "tool_calls", [{"id": "a", "function": {"name": "read", "arguments": "[]"}}]), "JSON object"),
    (completion("", "tool_calls", [{"id": "a", "function": {"name": "read", "arguments": "{}"}}] * 2), "duplicate"),
    ({"choices": []}, "choices"),
])
async def test_incomplete_or_invalid_completion_fails_once(data, pattern):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=data)
    with pytest.raises(LLMProviderError, match=pattern):
        await client_for(handler).generate(MODEL, "work")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 404, 429, 500])
async def test_http_errors_expose_status_not_body_or_secret(status):
    def handler(request):
        return httpx.Response(status, json={"error": KEY}, headers={"Location": f"http://different.test/?key={KEY}"})
    with pytest.raises(LLMProviderError, match=f"HTTP {status}") as caught:
        await client_for(handler).generate(MODEL, "work")
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert "different.test" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_error_redacts_exception_message():
    def handler(request):
        raise httpx.ConnectError(KEY, request=request)
    with pytest.raises(LLMProviderError, match="connection failed") as caught:
        await client_for(handler).list_models()
    assert KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_unverified_vision_and_gpu_options_fail_without_request(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_models", "")
    def forbidden(request):
        pytest.fail("must not send unsupported request")
    client = client_for(forbidden)
    with pytest.raises(LLMProviderError, match="not explicitly verified"):
        await client.chat(MODEL, "inspect", images=["data:image/png;base64,abcd"])
    with pytest.raises(LLMProviderError, match="unsupported inference option"):
        await client.generate(MODEL, "work", options={"num_gpu": 999})


def stream_events(*events, done=True):
    text = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return text + ("data: [DONE]\n\n" if done else "")


def delta(content=None, reason=None, **extra):
    return {"choices": [{"index": 0, "delta": {"content": content, **extra}, "finish_reason": reason}]}


@pytest.mark.asyncio
async def test_stream_yields_tokens_and_reasoning_and_validates_completion():
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=stream_events(delta(reasoning_content="consider"), delta("hello"), delta(reason="stop")), headers={"Content-Type": "text/event-stream"})
    assert [event async for event in client_for(handler).generate_stream(MODEL, "work")] == [
        {"kind": "think", "text": "consider"}, {"kind": "token", "text": "hello"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,pattern", [
    (stream_events(delta("partial"), done=False), "disconnected"),
    (stream_events(delta("partial"), delta(reason="stop"), done=False), "disconnected"),
    (stream_events(delta("partial")), "before finish reason"),
    (stream_events(delta("partial"), delta(reason="length")), "incomplete"),
    (stream_events(delta(reasoning_content="only reasoning"), delta(reason="stop")), "empty final"),
    ("data: not-json\n\n", "invalid stream JSON"),
])
async def test_stream_errors_preserve_partial_output_and_never_regenerate(payload, pattern):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=payload)
    partial = []
    with pytest.raises(LLMProviderError, match=pattern):
        async for event in client_for(handler).generate_stream(MODEL, "work"):
            partial.append(event)
    assert len(calls) == 1
    if '"partial"' in payload:
        assert partial == [{"kind": "token", "text": "partial"}]


def test_settings_secret_repr_and_remote_policy_are_safe():
    configuration = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=KEY)
    assert configuration.vram_policy == "independent"
    assert KEY not in repr(configuration)
    assert Settings(_env_file=None, llm_provider="ollama").vram_policy == "exclusive"
    with pytest.raises(ValueError, match="DS_VRAM_POLICY=independent") as caught:
        Settings(_env_file=None, llm_provider="openai_compatible", vram_policy="exclusive", llm_api_key=KEY)
    assert KEY not in str(caught.value)
