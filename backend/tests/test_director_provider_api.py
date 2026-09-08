from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import director as director_api
from app.api import health as health_api
from app.core import llm
from app.core.llm import LLMProviderError
from app.core.vram import director_model, orchestrator
from app.core.vram.director_model import ModelSelectionError


class Provider:
    provider_id = "openai_compatible"

    def __init__(self):
        self.current = "kasari-flash"
        self.list_models = AsyncMock(return_value=["kasari-flash", "kasari-brain"])
        self.select_model = Mock(side_effect=self._select)
        self.client = SimpleNamespace(generate=AsyncMock(return_value="Context loaded."))

    def _select(self, model, *, persist):
        self.current = model
        return model

    def model_status(self):
        return {"model": self.current, "source": "env", "persisted": None}

    def capabilities(self, model):
        return {"vision": model == "kasari-flash", "vision_reason": "Operator verification."}


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def client(provider):
    app = FastAPI()
    app.include_router(director_api.router, prefix="/api")
    app.dependency_overrides[director_api.get_llm_provider] = lambda: provider
    with TestClient(app) as client:
        yield client


def test_model_get_keeps_blank_selection_without_persisting(client, provider):
    provider.current = ""
    result = client.get("/api/director/model")
    assert result.status_code == 200
    assert result.json() == {
        "model": "",
        "source": "env",
        "persisted": None,
        "provider": "openai_compatible",
        "reachable": True,
        "available": ["kasari-flash", "kasari-brain"],
        "error": None,
        "capabilities": {"vision": False, "vision_reason": "Operator verification."},
    }
    provider.select_model.assert_not_called()


def test_unserved_selection_is_reported_without_replacing_it(client, provider):
    provider.current = "old-ollama-tag"
    body = client.get("/api/director/model").json()
    assert body["reachable"] is True
    assert body["model"] == "old-ollama-tag"
    assert "old-ollama-tag" in body["error"]
    assert "not served" in body["error"]
    provider.select_model.assert_not_called()


def test_malformed_selection_does_not_fall_back_to_env_or_first_model(client, provider):
    provider.model_status = Mock(side_effect=ModelSelectionError("Selection file is malformed."))
    body = client.get("/api/director/model").json()
    assert body["source"] == "error"
    assert body["model"] == ""
    assert body["error"] == "Selection file is malformed."
    assert body["available"] == ["kasari-flash", "kasari-brain"]
    provider.select_model.assert_not_called()


def test_catalog_failure_is_visible_and_preserves_selection(client, provider):
    provider.list_models.side_effect = LLMProviderError("openai_compatible: HTTP 401")
    body = client.get("/api/director/model").json()
    assert body["reachable"] is False
    assert body["model"] == "kasari-flash"
    assert body["available"] == []
    assert body["error"] == "openai_compatible: HTTP 401"
    provider.select_model.assert_not_called()


def test_model_put_checks_catalog_before_persist_and_returns_full_status(client, provider):
    response = client.put(
        "/api/director/model", json={"model": "kasari-brain", "persist": False}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "kasari-brain"
    assert body["reachable"] is True
    assert body["available"] == ["kasari-flash", "kasari-brain"]
    assert body["error"] is None
    assert body["capabilities"]["vision"] is False
    provider.list_models.assert_awaited_once()
    provider.select_model.assert_called_once_with("kasari-brain", persist=False)


@pytest.mark.parametrize("model", ["kasari", "KASARI-FLASH", "not-served", "   "])
def test_invalid_model_put_never_mutates_selection(client, provider, model):
    response = client.put("/api/director/model", json={"model": model})
    assert response.status_code == 400
    assert provider.current == "kasari-flash"
    provider.select_model.assert_not_called()


def test_empty_model_put_rejected_by_schema(client, provider):
    response = client.put("/api/director/model", json={"model": ""})
    assert response.status_code == 422
    provider.select_model.assert_not_called()


def test_failed_catalog_prevents_model_put(client, provider):
    provider.list_models.side_effect = LLMProviderError("openai_compatible: request timed out")
    response = client.put("/api/director/model", json={"model": "kasari-brain"})
    assert response.status_code == 503
    assert response.json()["detail"] == "openai_compatible: request timed out"
    provider.select_model.assert_not_called()


def test_raw_transport_error_never_leaks_into_model_response_or_logs(client, provider, caplog):
    unsafe = "PRIVATE_REQUEST_CONTENT_FOR_TEST"
    provider.list_models.side_effect = httpx.ConnectError(unsafe)
    get_result = client.get("/api/director/model")
    put_result = client.put("/api/director/model", json={"model": "kasari-brain"})
    assert "failed to connect" in get_result.json()["error"]
    assert "failed to connect" in put_result.json()["detail"]
    assert unsafe not in get_result.text + put_result.text + caplog.text


@pytest.fixture
def health_dependencies(provider, monkeypatch):
    monkeypatch.setattr(health_api, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(health_api.settings, "llm_provider", "openai_compatible")
    comfy = SimpleNamespace(status=AsyncMock(return_value=[{"id": "test", "status": "up", "error": None}]))
    monkeypatch.setattr(health_api, "get_worker_registry", lambda: comfy)
    constructor = Mock(side_effect=AssertionError("Health must not construct Ollama"))
    monkeypatch.setattr(llm, "OllamaLLMProvider", constructor)
    yield comfy
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_health_checks_selected_provider_and_model(provider, health_dependencies):
    body = await health_api.health()
    assert body.ok is True
    assert body.details["llm_provider"] == "openai_compatible"
    assert body.details["llm_reachable"] is True
    assert body.details["llm_model"] == "kasari-flash"
    assert body.details["llm_model_available"] is True
    assert body.details["llm_error"] is None
    assert "ollama_reachable" not in body.details
    provider.list_models.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", "no-longer-served"])
async def test_health_fails_when_selected_model_is_not_available(
    provider, health_dependencies, model
):
    provider.current = model
    body = await health_api.health()
    assert body.ok is False
    assert body.details["llm_reachable"] is True
    assert body.details["llm_model_available"] is False
    assert body.details["llm_error"]
    provider.select_model.assert_not_called()


@pytest.mark.asyncio
async def test_provider_health_failure_is_specific(provider, health_dependencies):
    provider.list_models.side_effect = LLMProviderError("openai_compatible: HTTP 401")
    body = await health_api.health()
    assert body.ok is False
    assert body.comfy_reachable is True
    assert body.details["llm_reachable"] is False
    assert body.details["llm_error"] == "openai_compatible: HTTP 401"


@pytest.mark.asyncio
async def test_comfy_health_failure_does_not_hide_llm_status(provider, health_dependencies):
    health_dependencies.status.side_effect = httpx.ConnectError("PRIVATE_REQUEST_CONTENT_FOR_TEST")
    body = await health_api.health()
    assert body.ok is False
    assert body.comfy_reachable is False
    assert body.comfy_error == "Comfy health check failed to connect."
    assert body.details["llm_reachable"] is True


@pytest.mark.asyncio
async def test_liveness_never_contacts_inference_providers(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Liveness must stay local"))
    monkeypatch.setattr(health_api, "get_worker_registry", forbidden)
    monkeypatch.setattr(health_api, "get_llm_provider", forbidden)
    assert await health_api.liveness() == {"ok": True}
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_independent_wake_uses_provider_without_claiming_model_release(
    provider, monkeypatch
):
    forbidden = Mock(side_effect=AssertionError("Independent wake cannot construct Ollama"))
    monkeypatch.setattr(orchestrator, "OllamaClient", forbidden)
    orch = orchestrator.VramOrchestrator(policy="independent", models=["kasari-flash"])
    monkeypatch.setattr(director_api, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(director_api, "get_director_model", lambda: "kasari-flash")
    monkeypatch.setattr(director_model, "get_director_model", lambda: "kasari-flash")
    monkeypatch.setattr(director_api, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(llm, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(
        director_api, "load_agent_context",
        lambda _: SimpleNamespace(project_id="project", last_phase="planning", shot_summaries=[]),
    )
    response = await director_api.wake_director(director_api.WakeBody(project_id="project"))
    assert response.llm_released is False
    assert response.agent_reply == "Context loaded."
    provider.client.generate.assert_awaited_once()
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_manual_free_is_explicit_even_under_independent_policy(monkeypatch):
    orch = SimpleNamespace(shared_gpu_enabled=False, release_comfy_models=AsyncMock())
    comfy = SimpleNamespace(free_memory=AsyncMock(return_value={"vram_free_gained": 5}))
    monkeypatch.setattr(director_api, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(director_api, "get_worker_registry", lambda: SimpleNamespace(client_for=lambda worker_id: comfy))
    assert await director_api.free_comfy_models(worker_id="test") == {
        "ok": True, "stats": {"vram_free_gained": 5}
    }
    comfy.free_memory.assert_awaited_once_with(unload_models=True, free_memory=True)
    orch.release_comfy_models.assert_not_called()


@pytest.mark.asyncio
async def test_manual_free_failure_is_not_success(monkeypatch):
    orch = SimpleNamespace(shared_gpu_enabled=False)
    comfy = SimpleNamespace(free_memory=AsyncMock(side_effect=httpx.ConnectError("private request")))
    monkeypatch.setattr(director_api, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(director_api, "get_worker_registry", lambda: SimpleNamespace(client_for=lambda worker_id: comfy))
    with pytest.raises(HTTPException) as error:
        await director_api.free_comfy_models(worker_id="test")
    assert error.value.status_code == 503
    assert error.value.detail == "Comfy model release failed to connect."
