from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.api.health import router as health_router
from app.config import settings


def test_server_enforces_one_worker_and_explicit_listener(monkeypatch):
    calls = []
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    monkeypatch.setattr(settings, "port", 18790)
    monkeypatch.setattr(settings, "reload", False)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))

    main.run()

    assert calls == [{"host": "127.0.0.1", "port": 18790, "reload": False, "workers": 1}]


def test_liveness_does_not_contact_inference_providers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Liveness must not depend on inference providers")

    monkeypatch.setattr("app.api.health.get_worker_registry", forbidden)
    monkeypatch.setattr("app.api.health.get_llm_provider", forbidden)
    app = FastAPI()
    app.include_router(health_router, prefix="/api")

    with TestClient(app) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
