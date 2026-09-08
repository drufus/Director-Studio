"""Remote renders must never suspend Director chat or evict remote models."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import llm
from app.core.vram import director_model, orchestrator
from app.core.vram.orchestrator import VramOrchestrator


@pytest.fixture
def remote_provider(monkeypatch):
    provider = SimpleNamespace(
        provider_id="openai_compatible",
        list_models=AsyncMock(return_value=["kasari-flash", "kasari-brain"]),
    )
    monkeypatch.setattr(llm, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(director_model, "get_director_model", lambda: "kasari-flash")
    return provider


@pytest.fixture
def independent(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Ollama must not be constructed"))
    monkeypatch.setattr(orchestrator, "OllamaClient", constructor)
    comfy = SimpleNamespace(free_memory=AsyncMock())
    orch = VramOrchestrator(
        policy="independent",
        models=["kasari-flash"],
        comfy=comfy,
        acquire_timeout_sec=0.05,
    )
    yield orch
    constructor.assert_not_called()
    comfy.free_memory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_on_exit", [False, True])
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        ("queued", "queued"),
        ("uploading", "uploading"),
        ("running", "generating"),
        ("running", "saving"),
    ],
)
async def test_remote_render_reservations_admit_llm_without_eviction(
    independent, remote_provider, status, phase, release_on_exit
):
    orch = independent
    await orch.reserve_generation(
        job_id="render-on-beastviii",
        pipeline_id="h3_ref2va",
        kind="video",
        status=status,
        phase=phase,
        queued_at="2026-09-08T00:00:00+00:00",
    )
    await orch.before_comfy_job("h3_ref2va")
    statuses = []

    async def plan():
        async with orch.llm_session(
            fail_if_generation_pending=True,
            release_on_exit=release_on_exit,
            on_status=statuses.append,
        ):
            await orch.ensure_llm_ready(on_status=statuses.append)
            return "shot plan"

    assert await asyncio.wait_for(plan(), timeout=0.5) == "shot plan"
    assert orch._waiters == 0
    assert orch.owner is None
    assert not orch.shared_gpu_enabled
    assert orch._llm_ready
    remote_provider.list_models.assert_awaited_once()
    assert statuses == ["kasari-flash available on openai_compatible"]
    reservations = await orch.generation_reservations()
    assert [(item.job_id, item.status, item.phase) for item in reservations] == [
        ("render-on-beastviii", status, phase)
    ]

    await orch.release_llm()
    assert await orch.release_comfy_models(require_ok=True) is None
    await orch.after_comfy_job("h3_ref2va", "succeeded")
    # Pipeline job lifecycle still owns reservations; admission never removes one.
    assert await orch.generation_reservations() == reservations
    await orch.release_generation("render-on-beastviii")
    assert await orch.generation_reservations() == []


@pytest.mark.asyncio
async def test_independent_workloads_can_overlap_without_gpu_ownership(
    independent, remote_provider
):
    orch = independent
    async with orch.llm_session():
        await orch.ensure_llm_ready()
        await asyncio.wait_for(orch.before_comfy_job("actor"), timeout=0.5)
        await asyncio.wait_for(orch.before_comfy_job("h3_ref2va"), timeout=0.5)
        async with orch.llm_session(fail_if_generation_pending=True):
            await orch.ensure_llm_ready()
            assert orch.owner is None
            assert orch.comfy_pipeline is None
        await orch.after_comfy_job("actor", "succeeded")
        await orch.after_comfy_job("h3_ref2va", "failed")
    assert remote_provider.list_models.await_count == 2


@pytest.mark.asyncio
async def test_independent_session_does_not_wait_for_an_owner(
    independent, remote_provider
):
    # Even an old owner value cannot impose a shared GPU lock in this policy.
    independent.owner = "comfy"
    independent.comfy_pipeline = "h3_ref2va"
    statuses = []
    async with independent.llm_session(
        fail_if_generation_pending=True, on_status=statuses.append
    ):
        await independent.ensure_llm_ready(on_status=statuses.append)
    assert not any("Waiting" in value or "Releasing" in value for value in statuses)
    assert independent._waiters == 0


@pytest.mark.asyncio
async def test_independent_catalog_failure_propagates_without_fallback(
    independent, remote_provider
):
    independent._llm_ready = True
    failure = RuntimeError("LiteLLM /v1/models returned HTTP 401")
    remote_provider.list_models.side_effect = failure
    with pytest.raises(RuntimeError, match="/v1/models returned HTTP 401") as error:
        async with independent.llm_session():
            await independent.ensure_llm_ready()
    assert error.value is failure
    assert not independent._llm_ready
    assert independent.owner is None


@pytest.mark.asyncio
async def test_independent_model_rechecked_and_never_substituted(
    independent, remote_provider
):
    async with independent.llm_session():
        await independent.ensure_llm_ready()
    remote_provider.list_models.return_value = ["kasari-brain"]
    with pytest.raises(
        RuntimeError, match="'kasari-flash' is not served by provider 'openai_compatible'"
    ):
        async with independent.llm_session():
            await independent.ensure_llm_ready()
    assert not independent._llm_ready
    assert independent.models == ["kasari-flash"]
    assert remote_provider.list_models.await_count == 2


@pytest.mark.asyncio
async def test_independent_blank_model_requires_selection(
    independent, remote_provider, monkeypatch
):
    monkeypatch.setattr(director_model, "get_director_model", lambda: "")
    with pytest.raises(RuntimeError, match="No Director model selected"):
        async with independent.llm_session():
            await independent.ensure_llm_ready()
    remote_provider.list_models.assert_not_called()


@pytest.mark.asyncio
async def test_independent_cancelled_chat_does_not_unload_models(
    independent, remote_provider
):
    entered = asyncio.Event()

    async def plan():
        async with independent.llm_session(release_on_exit=True):
            await independent.ensure_llm_ready()
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(plan())
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert independent.owner is None
    assert independent._llm_ready


def test_independent_singleton_does_not_construct_ollama(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Ollama must not be constructed"))
    monkeypatch.setattr(orchestrator, "OllamaClient", constructor)
    monkeypatch.setattr(orchestrator, "_orchestrator", None)
    monkeypatch.setattr(orchestrator.settings, "vram_policy", "independent")
    monkeypatch.setattr(director_model, "get_director_model", lambda: "kasari-flash")
    orch = orchestrator.get_orchestrator()
    assert not orch.shared_gpu_enabled
    assert orch.ollama is None
    assert orchestrator.get_orchestrator() is orch
    constructor.assert_not_called()


@pytest.mark.parametrize("policy", ["", "shared", "auto"])
def test_invalid_policy_rejected_before_constructing_clients(monkeypatch, policy):
    constructor = Mock()
    monkeypatch.setattr(orchestrator, "OllamaClient", constructor)
    with pytest.raises(ValueError, match="VRAM policy must be"):
        VramOrchestrator(policy=policy, models=[])
    constructor.assert_not_called()
