from __future__ import annotations

import json

import httpx
import pytest

from app.core.vram import ollama_client as ollama_module
from app.core.vram.ollama_client import OllamaClient
from app.core.llm.provider import LLMProviderError


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
@pytest.mark.parametrize("require_vision", [True, False])
async def test_require_vision_does_not_fall_back_to_text(monkeypatch, require_vision):
    monkeypatch.setattr(ollama_module.settings, "llm_vision_models", "ornith:35b")
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "does not support multimodal requests"})
    install_transport(monkeypatch, handler)
    with pytest.raises(LLMProviderError, match="HTTP 400"):
        await OllamaClient().chat("ornith:35b", "inspect", images=["abc"], require_vision=require_vision)
    assert len(calls) == 1
    assert calls[0]["messages"][-1]["images"] == ["abc"]


@pytest.mark.asyncio
async def test_ollama_closes_client_on_failure_and_redacts_error(monkeypatch):
    closed = []
    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("untrusted-provider-secret", request=request)
        async def aclose(self):
            closed.append(True)
    original = httpx.AsyncClient
    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=Transport()))
    with pytest.raises(LLMProviderError) as caught:
        await OllamaClient().chat_response("model", messages=[{"role": "user", "content": "hello"}])
    assert closed == [True]
    assert "untrusted-provider-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_ollama_standard_tool_replay_preserves_id_and_does_not_mutate_input(monkeypatch):
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "original_call", "type": "function", "function": {"name": "read", "arguments": '{"value":3}'}}]},
        {"role": "tool", "content": "ready", "tool_call_id": "original_call", "tool_name": "read"},
    ]
    def handler(request):
        body = json.loads(request.content)
        call = body["messages"][0]["tool_calls"][0]
        assert call["id"] == "original_call"
        assert call["function"]["arguments"] == {"value": 3}
        assert body["messages"][1]["tool_call_id"] == "original_call"
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": "", "tool_calls": [
            {"id": "provider_call", "function": {"name": "read", "arguments": {"value": 4}}}]}})
    install_transport(monkeypatch, handler)
    result = await OllamaClient().chat_response("model", messages=messages)
    assert result["tool_calls"] == [{"id": "provider_call", "name": "read", "arguments": {"value": 4}}]
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"value":3}'


@pytest.mark.asyncio
async def test_unverified_ollama_vision_is_rejected_before_inference(monkeypatch):
    monkeypatch.setattr(ollama_module.settings, "llm_vision_models", "")
    with pytest.raises(LLMProviderError, match="not explicitly verified"):
        await OllamaClient().chat("unknown", "inspect", images=["image"])


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"done": True, "done_reason": "length", "message": {"content": "partial"}},
    {"done": True, "done_reason": "stop", "message": {"thinking": "reasoning only"}},
    {"done": True, "done_reason": "stop", "message": {"tool_calls": [{"function": {"name": "read", "arguments": "bad"}}]}},
])
async def test_ollama_invalid_or_incomplete_completion_is_error(monkeypatch, result):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=result))
    with pytest.raises(LLMProviderError):
        await OllamaClient().chat_response("model", messages=[{"role": "user", "content": "work"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_ollama_generate_keeps_images_and_never_changes_protocol(monkeypatch, stream):
    monkeypatch.setattr(ollama_module.settings, "llm_vision_models", "vision")
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(400, json={"error": "unsupported multimodal"})
    install_transport(monkeypatch, handler)
    with pytest.raises((LLMProviderError, httpx.HTTPStatusError)):
        if stream:
            _ = [event async for event in OllamaClient().generate_stream("vision", "inspect", images=["base64-image"])]
        else:
            await OllamaClient().generate("vision", "inspect", images=["base64-image"])
    assert len(calls) == 1
    endpoint, body = calls[0]
    assert endpoint == ("/api/chat" if stream else "/api/generate")
    assert (body["messages"][-1] if stream else body)["images"] == ["base64-image"]
