from __future__ import annotations

import pytest

from app.api import director as director_api
from app.core.vram import GenerationReservation


class FakeOllama:
    async def loaded_models(self):
        return []

    async def model_vram_bytes(self, model: str) -> int:
        return 0


class FakeOrchestrator:
    owner = None
    comfy_pipeline = None
    policy = "exclusive"
    shared_gpu_enabled = True
    models = ["qwen-test"]
    acquire_timeout_sec = 3600
    _llm_ready = False
    _waiters = 0
    last_comfy_free = None
    last_comfy_free_error = None
    ollama = FakeOllama()

    async def generation_reservations(self):
        return [
            GenerationReservation(
                job_id="job_video",
                pipeline_id="h3_ref2va",
                kind="video",
                status="running",
                phase="generating",
                queued_at="2026-08-31T10:00:00+00:00",
            )
        ]


@pytest.mark.asyncio
async def test_vram_status_exposes_generation_chat_lock(monkeypatch):
    monkeypatch.setattr(director_api, "get_orchestrator", FakeOrchestrator)
    monkeypatch.setattr(director_api, "get_director_model", lambda: "qwen-test")

    result = await director_api.vram_status()

    assert result["chat_locked"] is True
    assert result["generation_count"] == 1
    assert result["generation_jobs"] == [
        {
            "job_id": "job_video",
            "pipeline_id": "h3_ref2va",
            "kind": "video",
            "status": "running",
            "phase": "generating",
            "queued_at": "2026-08-31T10:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["exclusive", "independent"])
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        ("queued", "queued"),
        ("uploading", "uploading"),
        ("running", "generating"),
        ("running", "saving"),
    ],
)
async def test_vram_status_only_locks_chat_for_shared_gpu(
    monkeypatch, policy, status, phase
):
    class Orchestrator(FakeOrchestrator):
        @property
        def ollama(self):
            assert self.shared_gpu_enabled, "Independent status must not access Ollama"
            return FakeOllama()

        async def generation_reservations(self):
            return [
                GenerationReservation(
                    job_id="remote-render",
                    pipeline_id="h3_ref2va",
                    kind="video",
                    status=status,
                    phase=phase,
                    queued_at="2026-09-08T00:00:00+00:00",
                )
            ]

    orch = Orchestrator()
    orch.policy = policy
    orch.shared_gpu_enabled = policy == "exclusive"
    orch._llm_ready = True
    monkeypatch.setattr(director_api, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(director_api, "get_director_model", lambda: "kasari-flash")

    result = await director_api.vram_status()

    assert result["policy"] == policy
    assert result["chat_locked"] is (policy == "exclusive")
    assert result["shared_gpu_enabled"] is (policy == "exclusive")
    assert result["generation_count"] == 1
    assert result["generation_jobs"][0]["status"] == status
    assert result["generation_jobs"][0]["phase"] == phase
    if policy == "independent":
        assert result["ollama_size_vram"] is None
        assert result["ollama_on_gpu"] is None
        assert result["ollama_ps"] == []
        assert result["llm_keep_loaded"] is False
        assert result["llm_ready"] is True
