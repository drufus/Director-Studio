"""Regression tests at the Director/provider boundary, independent of HTTP wire parsing."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api import projects
from app.core.llm import LLMProviderError


@pytest.fixture
def routing(monkeypatch):
    @asynccontextmanager
    async def session(**kwargs):
        yield

    async def ready(**kwargs):
        return None

    orch = SimpleNamespace(llm_session=session, ensure_llm_ready=ready)
    monkeypatch.setattr("app.core.vram.get_orchestrator", lambda: orch)
    selected = ["original-model"]
    monkeypatch.setattr("app.core.vram.director_model.get_director_model", lambda: selected[0])
    return selected


@pytest.mark.asyncio
async def test_partial_stream_failure_never_regenerates(routing, monkeypatch):
    calls = []
    progress = []

    class Client:
        async def generate_stream(self, model, prompt, **kwargs):
            calls.append(("stream", model))
            yield {"kind": "token", "text": "Partial answer"}
            raise LLMProviderError("openai_compatible: stream disconnected")

        async def generate(self, *args, **kwargs):
            raise AssertionError("Must not regenerate after partial output")

    monkeypatch.setattr(projects, "get_llm_provider", lambda: SimpleNamespace(client=Client()))

    async def observe(event):
        progress.append(event)

    chat = await projects._make_chat_fn(on_progress=observe)
    with pytest.raises(LLMProviderError, match="disconnected"):
        await chat("system", "user")
    assert calls == [("stream", "original-model")]
    assert {"type": "token", "text": "Partial answer"} in progress


@pytest.mark.asyncio
async def test_conversation_and_plan_repairs_pin_selected_model(routing, monkeypatch):
    calls = []

    class Client:
        async def generate(self, model, prompt):
            calls.append(model)
            return "answer"

    monkeypatch.setattr(projects, "get_llm_provider", lambda: SimpleNamespace(client=Client()))
    chat = await projects._make_chat_fn()
    planner = projects.DirectorPlanProvider()
    routing[0] = "new-selection"
    await chat("system", "first turn")
    await chat("system", "follow-up")
    await planner.complete("system", "plan")
    await planner.complete("system", "repair malformed JSON")
    assert calls == ["original-model"] * 4


@pytest.mark.asyncio
async def test_unverified_vision_stops_before_inference(routing, monkeypatch):
    class Client:
        async def chat_response(self, *args, **kwargs):
            raise AssertionError("Unverified image input must not be submitted")

    monkeypatch.setattr(projects, "get_llm_provider", lambda: SimpleNamespace(
        client=Client(), capabilities=lambda model: {"vision": False, "vision_reason": "Images unverified for original-model"},
    ))
    chat = await projects._make_chat_fn()
    with pytest.raises(LLMProviderError, match="Images unverified"):
        await chat("system", "inspect", messages=[{"role": "user", "content": "inspect", "images": ["encoded"]}])
