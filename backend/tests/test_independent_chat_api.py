"""HTTP admission, real agent inference, and residency stay independent of renders."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.agents.director.service import DirectorService
from app.api import director as director_api
from app.api import projects as projects_api
from app.config import settings
from app.core import llm
from app.core.llm import ollama as ollama_provider
from app.core.projects.chat_history import load_chat_history
from app.core.projects.chat_sessions import DirectorChatSessionRegistry
from app.core.projects.store import create_project
from app.core.vram import director_model, orchestrator
from app.core.vram.orchestrator import VramOrchestrator


MODEL = "verified-remote-model"
REPLY = "The quiet opening gives the audience time to understand the setting."
MESSAGE = "How could we establish a quieter opening?"


@pytest.fixture
def remote_chat_app(tmp_path, monkeypatch):
    for field, name in [
        ("projects_dir", "projects"), ("jobs_dir", "jobs"),
        ("library_root", "library"), ("library_dir", "library/actors"),
    ]:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, field, directory)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", MODEL)
    monkeypatch.setattr(settings, "llm_vision_models", MODEL)
    monkeypatch.setattr(settings, "vram_policy", "independent")
    # Exercise the release-on-exit branch as well as admission: remote calls
    # must remain unable to unload models even when residency is disabled.
    monkeypatch.setattr(settings, "llm_keep_loaded", False)
    monkeypatch.setattr(director_model, "get_director_model", lambda: MODEL)
    monkeypatch.setattr(director_api, "get_director_model", lambda: MODEL)
    monkeypatch.setattr(projects_api, "director_chat_sessions", DirectorChatSessionRegistry())

    forbidden_ollama = Mock(side_effect=AssertionError("Remote chat must never construct Ollama"))
    monkeypatch.setattr(orchestrator, "OllamaClient", forbidden_ollama)
    monkeypatch.setattr(ollama_provider, "OllamaClient", forbidden_ollama)
    comfy_free = AsyncMock(side_effect=AssertionError("Remote chat must never evict Comfy models"))
    orch = VramOrchestrator(
        policy="independent", models=[MODEL],
        comfy=SimpleNamespace(free_memory=comfy_free), acquire_timeout_sec=0.02,
    )
    monkeypatch.setattr(orchestrator, "_orchestrator", orch)
    inference = AsyncMock(return_value={"content": REPLY, "thinking": "", "tool_calls": []})
    provider = SimpleNamespace(
        provider_id="openai_compatible",
        client=SimpleNamespace(chat_response=inference),
        list_models=AsyncMock(return_value=[MODEL]),
        capabilities=Mock(return_value={"vision": True, "vision_reason": "Verified test capability."}),
    )
    monkeypatch.setattr(llm, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(projects_api, "get_llm_provider", lambda: provider)

    # Mount the production routers without application lifespan, which performs
    # unrelated service startup and Comfy discovery. The chat path itself is real.
    app = FastAPI()
    app.include_router(projects_api.router, prefix="/api")
    app.include_router(director_api.router, prefix="/api")
    app.state.director_service = DirectorService(
        plan_provider=SimpleNamespace(complete=AsyncMock(side_effect=AssertionError("No planner shortcut expected"))),
        orchestrator=orch,
    )
    project = create_project("Independent remote conversation", "A visitor arrives quietly.")
    yield SimpleNamespace(app=app, orch=orch, project=project, provider=provider, inference=inference)
    forbidden_ollama.assert_not_called()
    comfy_free.assert_not_awaited()


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color="red").save(output, format="PNG")
    return output.getvalue()


def _sse_events(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["json", "sse", "images"])
@pytest.mark.parametrize(
    ("status", "phase"),
    [("queued", "queued"), ("uploading", "uploading"), ("running", "generating")],
)
async def test_chat_http_completes_while_remote_render_remains_reserved(
    remote_chat_app, status, phase, transport
):
    env = remote_chat_app
    queued_at = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    await env.orch.reserve_generation(
        job_id="beastviii-long-h3-render", pipeline_id="h3_ref2va", kind="video",
        status=status, phase=phase, queued_at=queued_at,
    )
    if status == "running":
        await env.orch.before_comfy_job("h3_ref2va")
    expected_reservations = await env.orch.generation_reservations()
    image_bytes = _png_bytes()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://testserver"
    ) as client:
        before = await client.get("/api/director/vram")
        assert before.status_code == 200
        assert before.json()["chat_locked"] is False
        assert before.json()["generation_count"] == 1
        assert before.json()["generation_jobs"][0]["status"] == status
        base_url = f"/api/projects/{env.project.id}/chat"
        if transport == "images":
            request = client.post(
                base_url + "/stream/images", data={"message": MESSAGE, "history": "[]"},
                files=[("images", ("frame.png", image_bytes, "image/png"))],
            )
        elif transport == "sse":
            request = client.post(base_url + "/stream", json={"message": MESSAGE})
        else:
            request = client.post(base_url, json={"message": MESSAGE})
        response = await asyncio.wait_for(request, timeout=2)
        assert response.status_code == 200
        if transport == "json":
            payload = response.json()
        else:
            events = _sse_events(response)
            assert not [event for event in events if event["type"] == "error"], events
            results = [event["data"] for event in events if event["type"] == "result"]
            assert len(results) == 1
            payload = results[0]
            assert any(event["type"] == "token" and event["text"] == REPLY for event in events)
        assert payload["reply"] == REPLY
        assert "llm" in payload["actions"]
        assert (await client.get(base_url + "/session")).json()["active"] is False
        after = (await client.get("/api/director/vram")).json()
        assert after["chat_locked"] is False
        assert after["generation_jobs"] == before.json()["generation_jobs"]

    env.provider.list_models.assert_awaited_once()
    env.inference.assert_awaited_once()
    call = env.inference.await_args
    assert call.args == (MODEL,)
    assert call.kwargs["tools"]  # Exercises native chat through the real adapter boundary.
    if transport == "images":
        assert call.kwargs["require_vision"] is True
        submitted_images = [image for message in call.kwargs["messages"] for image in message.get("images", [])]
        assert len(submitted_images) == 1
        with Image.open(io.BytesIO(base64.b64decode(submitted_images[0]))) as decoded:
            assert decoded.size == (16, 16)
            assert decoded.getpixel((0, 0)) == (255, 0, 0)
        env.provider.capabilities.assert_called_once_with(MODEL)
        assert len(load_chat_history(env.project.id)[0].images) == 1
    else:
        assert call.kwargs["require_vision"] is False
    assert [(entry.role, entry.content) for entry in load_chat_history(env.project.id)] == [
        ("user", MESSAGE), ("assistant", REPLY),
    ]
    assert await env.orch.generation_reservations() == expected_reservations
    assert env.orch.owner is None
    assert env.orch._waiters == 0


@pytest.mark.asyncio
async def test_remote_render_policy_preserves_per_project_active_chat_lock(remote_chat_app):
    env = remote_chat_app
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_reply(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"content": REPLY, "thinking": "", "tool_calls": []}

    env.inference.side_effect = hold_reply
    await env.orch.reserve_generation(
        job_id="remote-render", pipeline_id="h3_ref2va", kind="video",
        status="running", phase="generating", queued_at=datetime.now(timezone.utc).isoformat(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://testserver"
    ) as client:
        endpoint = f"/api/projects/{env.project.id}/chat/stream"
        active = asyncio.create_task(client.post(endpoint, json={"message": MESSAGE}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            conflict = await client.post(endpoint, json={"message": "A second request."})
            assert conflict.status_code == 409
            assert "already running" in conflict.json()["detail"]
            assert (await client.get("/api/director/vram")).json()["chat_locked"] is False
        finally:
            release.set()
            completed = await asyncio.wait_for(active, timeout=2)
        assert any(event["type"] == "result" for event in _sse_events(completed))
        env.inference.assert_awaited_once()
