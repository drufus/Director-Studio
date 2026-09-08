"""Provider token accounting is server-reported, request-scoped, and content-free."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import settings
from app.core.llm import LLMProviderError
from app.core.llm import openai_compatible as transport_module
from app.core.llm.openai_compatible import OpenAICompatibleClient
from app.core.vram.director_model import ModelSelectionError

MODEL = "usage-test-model"
SECRET = "private-test-credential"


@pytest.fixture(autouse=True)
def selection(monkeypatch):
    state = {"model": MODEL, "source": "env"}
    monkeypatch.setattr(transport_module, "model_status", lambda **kwargs: dict(state))
    monkeypatch.setattr(settings, "director_num_predict", 8192)
    return state


def completion(*, usage=None, reason="stop", content="Completed output"):
    result = {"choices": [{
        "message": {"role": "assistant", "content": content, "reasoning_content": "private reasoning text"},
        "finish_reason": reason,
    }]}
    if usage is not None:
        result["usage"] = usage
    return result


def client_for(handler):
    return OpenAICompatibleClient(
        "http://usage.test/v1", api_key=SECRET, transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_completed_response_reports_server_reasoning_and_payload_counts(caplog):
    data = completion(usage={
        "prompt_tokens": 1100, "completion_tokens": 8192, "total_tokens": 9292,
        "completion_tokens_details": {"reasoning_tokens": 7000},
    })
    client = client_for(lambda request: httpx.Response(200, json=data))
    with caplog.at_level(logging.INFO, logger="director_studio.llm.usage"):
        assert await client.generate(MODEL, "private input prompt") == "Completed output"
    assert client.last_usage == {
        "provider": "openai_compatible", "model": MODEL, "selection_source": "env",
        "operation": "chat_completion", "max_tokens": 8192, "finish_reason": "stop", "status": "succeeded",
        "prompt_tokens": 1100, "completion_tokens": 8192, "total_tokens": 9292,
        "reasoning_tokens": 7000, "payload_tokens": 1192,
    }
    event = next(record.message for record in caplog.records if record.name == "director_studio.llm.usage")
    assert json.loads(event.removeprefix("inference_usage ")) == client.last_usage
    for private in (SECRET, "private input prompt", "private reasoning text", "Completed output"):
        assert private not in event


@pytest.mark.asyncio
async def test_length_failure_retains_usage_before_validation_and_never_retries():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion(reason="length", content="partial", usage={
            "prompt_tokens": 300, "completion_tokens": 8192, "total_tokens": 8492,
            "completion_tokens_details": {"reasoning_tokens": 8192},
        }))
    client = client_for(handler)
    with pytest.raises(LLMProviderError, match="finish_reason=length"):
        await client.generate(MODEL, "work")
    assert len(calls) == 1
    assert client.last_usage["status"] == "failed"
    assert client.last_usage["finish_reason"] == "length"
    assert client.last_usage["reasoning_tokens"] == 8192
    assert client.last_usage["payload_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("usage", "expected"), [
    (None, (None, None, None)),
    ({"completion_tokens": 8192}, (8192, None, None)),
    ({"completion_tokens": 8192, "completion_tokens_details": {}}, (8192, None, None)),
    ({"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 0}}, (8192, 0, 8192)),
    ({"completion_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0}}, (0, 0, 0)),
    ({"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 11}}, (10, 11, None)),
    ({"completion_tokens": True, "completion_tokens_details": {"reasoning_tokens": False}}, (None, None, None)),
    ({"completion_tokens": "8192", "completion_tokens_details": {"reasoning_tokens": -1}}, (None, None, None)),
    ({"total_tokens": 9000, "completion_tokens_details": {"reasoning_tokens": 100}}, (None, 100, None)),
])
async def test_missing_or_invalid_breakdown_never_infers_token_counts(usage, expected):
    client = client_for(lambda request: httpx.Response(200, json=completion(usage=usage)))
    await client.generate(MODEL, "work")
    assert tuple(client.last_usage[key] for key in ("completion_tokens", "reasoning_tokens", "payload_tokens")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["env", "persisted", "runtime"])
async def test_selection_source_is_captured_before_request_await(selection, source):
    selection["source"] = source
    async def handler(request):
        selection.update(model="changed-while-inference-ran", source="runtime")
        return httpx.Response(200, json=completion())
    client = client_for(handler)
    await client.generate(MODEL, "work", options={"max_tokens": 1024})
    assert client.last_usage["model"] == MODEL
    assert client.last_usage["selection_source"] == source
    assert client.last_usage["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_request_for_a_different_model_is_explicit_without_reselecting(selection):
    client = client_for(lambda request: httpx.Response(200, json=completion()))
    await client.generate("explicit-diagnostic-model", "work")
    assert client.last_usage["model"] == "explicit-diagnostic-model"
    assert client.last_usage["selection_source"] == "explicit_request"
    assert selection == {"model": MODEL, "source": "env"}


@pytest.mark.asyncio
async def test_unreadable_selection_does_not_invent_a_source_or_send_request(monkeypatch):
    def broken(**kwargs):
        raise ModelSelectionError("Director model selection file is malformed.")
    monkeypatch.setattr(transport_module, "model_status", broken)
    calls = []
    client = client_for(lambda request: calls.append(request))
    with pytest.raises(ModelSelectionError, match="malformed"):
        await client.generate(MODEL, "work")
    assert calls == []
    assert client.last_usage is None


def stream_payload(reason="stop", *, include_usage=True, done=True):
    events = [
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": reason}]},
    ]
    if include_usage:
        events.append({"choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292,
            "completion_tokens_details": {"reasoning_tokens": 8100},
        }})
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + ("data: [DONE]\n\n" if done else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_stream_collects_usage_trailer_after_finish_before_raising(reason):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 8192
        return httpx.Response(200, text=stream_payload(reason), headers={"Content-Type": "text/event-stream"})
    client = client_for(handler)
    chunks = []
    async def consume():
        async for chunk in client.generate_stream(MODEL, "work"):
            chunks.append(chunk)
    if reason == "length":
        with pytest.raises(LLMProviderError, match="finish_reason=length"):
            await consume()
    else:
        await consume()
    assert chunks == [{"kind": "token", "text": "partial"}]
    assert len(requests) == 1
    assert client.last_usage["status"] == ("succeeded" if reason == "stop" else "failed")
    assert client.last_usage["reasoning_tokens"] == 8100
    assert client.last_usage["payload_tokens"] == 92
    assert client.last_usage["finish_reason"] == reason
    assert client.last_usage["operation"] == "stream_completion"


@pytest.mark.asyncio
async def test_disconnected_stream_keeps_unknown_counts_and_logs_no_partial_text(caplog):
    client = client_for(lambda request: httpx.Response(200, text=stream_payload(include_usage=False, done=False)))
    with caplog.at_level(logging.INFO, logger="director_studio.llm.usage"):
        with pytest.raises(LLMProviderError, match="disconnected"):
            async for _ in client.generate_stream(MODEL, "work"):
                pass
    assert client.last_usage["status"] == "failed"
    assert client.last_usage["completion_tokens"] is None
    assert client.last_usage["reasoning_tokens"] is None
    assert client.last_usage["payload_tokens"] is None
    event = next(record.message for record in caplog.records if record.name == "director_studio.llm.usage")
    assert "partial" not in event
    assert SECRET not in event


@pytest.mark.asyncio
async def test_failure_metrics_never_include_arbitrary_response_fields_or_credentials(caplog):
    data = completion(usage={"completion_tokens": 1, "secret": SECRET}, content="")
    data["choices"][0]["finish_reason"] = SECRET
    client = client_for(lambda request: httpx.Response(200, json=data))
    with caplog.at_level(logging.INFO, logger="director_studio.llm.usage"):
        with pytest.raises(LLMProviderError):
            await client.generate(MODEL + SECRET, "work")
    event = next(record.message for record in caplog.records if record.name == "director_studio.llm.usage")
    assert SECRET not in event
    assert "[REDACTED]" in event
    assert "private reasoning text" not in event
    assert client.last_usage["finish_reason"] is None
    assert client.last_usage["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_litellm_empty_choice_usage_footer_is_accounting_only(reason):
    footer = {"choices": [{"index": 0, "delta": {}, "finish_reason": None}], "usage": {
        "prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292,
        "completion_tokens_details": {"reasoning_tokens": 8000},
    }}
    payload = stream_payload(reason, include_usage=False, done=False) + f"data: {json.dumps(footer)}\n\ndata: [DONE]\n\n"
    client = client_for(lambda request: httpx.Response(200, text=payload))
    chunks = []
    async def consume():
        async for chunk in client.generate_stream(MODEL, "work"):
            chunks.append(chunk)
    if reason == "length":
        with pytest.raises(LLMProviderError, match="finish_reason=length"):
            await consume()
    else:
        await consume()
    assert chunks == [{"kind": "token", "text": "partial"}]
    assert client.last_usage["finish_reason"] == reason
    assert client.last_usage["payload_tokens"] == 192
    assert client.last_usage["status"] == ("succeeded" if reason == "stop" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [
    {"content": "additional text"}, {"content": ""},
    {"reasoning_content": "additional reasoning"},
    {"tool_calls": [{"id": "call_after_finish"}]},
    {"function_call": {"name": "legacy_call"}}, {"refusal": "refused"},
])
async def test_usage_footer_cannot_add_model_output_after_finish(delta):
    footer = {"choices": [{"delta": delta, "finish_reason": None}], "usage": {"completion_tokens": 5}}
    payload = stream_payload(include_usage=False, done=False) + f"data: {json.dumps(footer)}\n\ndata: [DONE]\n\n"
    client = client_for(lambda request: httpx.Response(200, text=payload))
    chunks = []
    with pytest.raises(LLMProviderError):
        async for chunk in client.generate_stream(MODEL, "work"):
            chunks.append(chunk)
    assert chunks == [{"kind": "token", "text": "partial"}]
    assert client.last_usage["status"] == "failed"
    assert client.last_usage["finish_reason"] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("footer", [
    {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"completion_tokens": 5}},
    {"choices": [{"delta": {}, "finish_reason": None, "index": 1}], "usage": {"completion_tokens": 5}},
    {"choices": [{"delta": {}, "finish_reason": None}]},
])
async def test_post_finish_choice_must_be_the_verified_accounting_shape(footer):
    payload = stream_payload(include_usage=False, done=False) + f"data: {json.dumps(footer)}\n\ndata: [DONE]\n\n"
    client = client_for(lambda request: httpx.Response(200, text=payload))
    with pytest.raises(LLMProviderError, match="after finish reason"):
        async for _ in client.generate_stream(MODEL, "work"):
            pass
    assert client.last_usage["status"] == "failed"
    assert client.last_usage["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_usage_footer_does_not_allow_duplicate_done_marker():
    footer = {"choices": [{"delta": {}, "finish_reason": None}], "usage": {"completion_tokens": 5}}
    payload = stream_payload(include_usage=False, done=False) + f"data: {json.dumps(footer)}\n\ndata: [DONE]\n\ndata: [DONE]\n\n"
    client = client_for(lambda request: httpx.Response(200, text=payload))
    with pytest.raises(LLMProviderError, match="duplicate stream completion marker"):
        async for _ in client.generate_stream(MODEL, "work"):
            pass
    assert client.last_usage["status"] == "failed"
