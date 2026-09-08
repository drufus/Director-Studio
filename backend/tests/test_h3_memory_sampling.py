"""Observed pool peaks stay durable without changing remote prompt ownership."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core.comfy.client import ComfyError
from app.core.comfy.memory import H3MemorySampler, memory_snapshot
from app.core.jobs import store
from app.core.schemas import JobStatus, OutputSlot

GIB = 1024**3


def stats(free, *, ram=1, total=128):
    return {"system": {"ram_free": ram * GIB, "ram_total": total * GIB},
            "devices": [{"index": 0, "name": "GB10", "vram_free": free * GIB, "vram_total": total * GIB}]}


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    job = store.create_job(pipeline_id="h3_ref2va", asset_kind="tests", name="Memory calibration")
    job.worker_id, job.worker_url = "worker", "http://worker.test:8188"
    job.status = JobStatus.running
    store.save_job(job)
    return job


@pytest.mark.asyncio
async def test_interval_peak_is_durable_before_completion_without_clobbering_job(job):
    at_peak = asyncio.Event()
    samples = 0

    async def health(**kwargs):
        nonlocal samples
        samples += 1
        if samples == 2:
            at_peak.set()
            return stats(7)
        return stats(32)

    sampler = H3MemorySampler(job, SimpleNamespace(health=health), interval=0.001)
    await sampler.start()
    durable = store.load_job(job.id)
    durable.status, durable.comfy_prompt_id = JobStatus.cancelled, "pinned-prompt"
    durable.outputs = {"video": OutputSlot(key="video", label="Video", filename="kept.mp4")}
    store.save_job(durable)
    await asyncio.wait_for(at_peak.wait(), timeout=1)
    ongoing = store.load_job(job.id)
    assert ongoing.memory_usage["devices"][0]["min_vram_free_bytes"] == 7 * GIB
    assert ongoing.memory_usage["devices"][0]["peak_vram_used_bytes"] == 121 * GIB
    assert ongoing.memory_usage["devices"][0]["peak_vram_delta_bytes"] == 25 * GIB
    assert ongoing.status == JobStatus.cancelled
    assert ongoing.comfy_prompt_id == "pinned-prompt"
    assert ongoing.outputs["video"].filename == "kept.mp4"
    await sampler.finish("cancelled")
    final = store.load_job(job.id).memory_usage
    assert final["status"] == "completed"
    assert final["sample_count"] >= 3
    assert final["samples"][0]["phase"] == "submit"
    assert final["samples"][-1]["phase"] == "cancelled"
    assert final["devices"][0]["peak_at"]


@pytest.mark.asyncio
async def test_failed_sample_is_visible_immediately_and_later_samples_continue(job):
    health = AsyncMock(side_effect=[stats(32), ComfyError("worker telemetry HTTP 503"), stats(10), stats(24)])
    sampler = H3MemorySampler(job, SimpleNamespace(health=health), interval=60)
    await sampler.start()
    await sampler.sample("execution")
    failed = store.load_job(job.id).memory_usage
    assert failed["status"] == "incomplete"
    assert "HTTP 503" in failed["errors"][0]["error"]
    await sampler.sample("execution")
    await sampler.finish("completed")
    final = store.load_job(job.id).memory_usage
    assert final["sample_count"] == 3
    assert final["devices"][0]["min_vram_free_bytes"] == 10 * GIB
    with pytest.raises(ComfyError, match="render completed.*measurements are incomplete.*HTTP 503"):
        sampler.require_complete()


@pytest.mark.asyncio
async def test_resume_preserves_baseline_and_peak_and_marks_unobserved_interval(job):
    original = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(side_effect=[stats(32), stats(9)])), interval=60)
    await original.start()
    await original.finish("interrupted")
    before = store.load_job(job.id).memory_usage
    resumed = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(side_effect=[stats(20), stats(18)])), interval=60)
    await resumed.start(resume=True)
    await resumed.finish("completed")
    after = store.load_job(job.id).memory_usage
    assert after["started_at"] == before["started_at"]
    assert after["sample_count"] == 4
    assert after["devices"][0]["baseline_vram_free_bytes"] == 32 * GIB
    assert after["devices"][0]["min_vram_free_bytes"] == 9 * GIB
    assert after["devices"][0]["peak_vram_delta_bytes"] == 23 * GIB
    assert after["status"] == "incomplete"
    assert "unobserved interval" in after["errors"][0]["error"]


@pytest.mark.asyncio
async def test_missing_submit_vram_is_fatal_and_persists_cause(job):
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(return_value={"devices": []})))
    with pytest.raises(ComfyError, match="submit memory measurement failed.*no GPU devices"):
        await sampler.start()
    usage = store.load_job(job.id).memory_usage
    assert usage["status"] == "incomplete"
    assert usage["sample_count"] == 0
    assert "no GPU devices" in usage["errors"][0]["error"]


@pytest.mark.asyncio
async def test_multiple_devices_keep_independent_peaks_not_summed_pools(job):
    first, second = stats(32), stats(20)
    first["devices"].append({"index": 1, "name": "GPU1", "vram_free": 8 * GIB, "vram_total": 24 * GIB})
    second["devices"].append({"index": 1, "name": "GPU1", "vram_free": 12 * GIB, "vram_total": 24 * GIB})
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(side_effect=[first, second])), interval=60)
    await sampler.start()
    await sampler.finish("completed")
    usage = store.load_job(job.id).memory_usage
    assert [d["peak_vram_used_bytes"] for d in usage["devices"]] == [108 * GIB, 16 * GIB]
    assert [d["peak_vram_delta_bytes"] for d in usage["devices"]] == [12 * GIB, 0]
    assert "peak_vram_used_bytes" not in usage


@pytest.mark.asyncio
async def test_gpu_total_change_is_an_explicit_incomplete_measurement(job):
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(side_effect=[stats(32), stats(16, total=64)])), interval=60)
    await sampler.start()
    await sampler.finish("completed")
    usage = store.load_job(job.id).memory_usage
    assert usage["status"] == "incomplete"
    assert "total memory changed" in usage["errors"][0]["error"]
    assert usage["sample_count"] == 1


@pytest.mark.parametrize("ram", [None, -1, float("nan"), False])
def test_ram_is_informational_and_does_not_gate_gpu_telemetry(ram):
    payload = stats(32)
    payload["system"]["ram_free"] = ram
    measurement = memory_snapshot(payload, phase="submit")
    assert measurement["ram_free_bytes"] is None
    assert measurement["devices"][0]["vram_free_bytes"] == 32 * GIB


@pytest.mark.asyncio
async def test_safe_unsubmitted_replay_starts_a_new_submit_baseline(job):
    first = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(return_value=stats(32))), interval=60)
    await first.start()
    await first.finish("interrupted")
    replay = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(return_value=stats(20))), interval=60)
    await replay.start()
    await replay.finish("completed")
    usage = store.load_job(job.id).memory_usage
    assert usage["sample_count"] == 2
    assert usage["devices"][0]["baseline_vram_free_bytes"] == 20 * GIB
    assert usage["errors"] == []


@pytest.mark.asyncio
async def test_interruption_during_finalization_keeps_sampler_available_for_cleanup(job):
    final_request = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def health(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            final_request.set()
            await release.wait()
        return stats(32 if calls == 1 else 10)

    sampler = H3MemorySampler(job, SimpleNamespace(health=health), interval=0.001)
    await sampler.start()
    await asyncio.wait_for(final_request.wait(), timeout=1)
    finish_task = asyncio.create_task(sampler.finish("completed"))
    await asyncio.sleep(0)
    finish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish_task
    release.set()
    await sampler.finish("interrupted")
    usage = store.load_job(job.id).memory_usage
    assert usage["status"] == "interrupted"
    assert usage["finished_at"]
    assert usage["sample_count"] == 3
    assert usage["devices"][0]["min_vram_free_bytes"] == 10 * GIB


@pytest.mark.asyncio
async def test_slow_samples_use_configured_timeout_without_changing_fleet_health(job, monkeypatch):
    from datetime import datetime

    import httpx

    from app.core.comfy.client import ComfyClient

    monkeypatch.setattr(settings, "comfy_memory_request_timeout_sec", 30)
    requests = []

    async def delayed_stats(request):
        requests.append(request.extensions["timeout"]["read"])
        # A delayed response must retain its real request/observation timestamps;
        # the configured sample cadence never invents samples during the wait.
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=stats(32))

    client = ComfyClient(job.worker_url, worker_id=job.worker_id, transport=httpx.MockTransport(delayed_stats))
    await client.health()
    sampler = H3MemorySampler(job, client, interval=60)
    await sampler.start()
    await sampler.finish("completed")
    assert requests == [5, 30, 30]
    usage = store.load_job(job.id).memory_usage
    assert usage["sample_count"] == 2
    assert usage["request_timeout_sec"] == 30
    assert usage["errors"] == []
    for sample in usage["samples"]:
        assert sample["request_duration_sec"] >= 0.02
        assert datetime.fromisoformat(sample["sampled_at"]) > datetime.fromisoformat(sample["requested_at"])


@pytest.mark.asyncio
async def test_request_timeout_is_recorded_once_without_retry_or_erasing_prior_error(job, monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "comfy_memory_request_timeout_sec", 12)
    health = AsyncMock(side_effect=[stats(32), httpx.ReadTimeout("stats deadline exceeded"), stats(20)])
    sampler = H3MemorySampler(job, SimpleNamespace(health=health), interval=60)
    await sampler.start()
    await sampler.sample("execution")
    incomplete = store.load_job(job.id).memory_usage
    assert health.await_count == 2
    assert incomplete["sample_count"] == 1
    assert len(incomplete["errors"]) == 1
    assert "stats deadline exceeded" in incomplete["errors"][0]["error"]
    assert incomplete["errors"][0]["requested_at"]
    assert incomplete["errors"][0]["request_duration_sec"] >= 0
    await sampler.finish("completed")
    final = store.load_job(job.id).memory_usage
    assert final["errors"] == incomplete["errors"]
    assert final["sample_count"] == 2
    assert final["status"] == "incomplete"
    assert health.await_count == 3
    assert all(call.kwargs == {"timeout": 12} for call in health.await_args_list)


@pytest.mark.asyncio
async def test_resume_preserves_completed_render_measurements_without_resampling(job):
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(side_effect=[stats(32), stats(10)])), interval=60)
    await sampler.start()
    await sampler.finish("completed")
    completed = store.load_job(job.id).memory_usage
    health = AsyncMock(side_effect=AssertionError("Finished render must not restart memory observations"))
    resumed = H3MemorySampler(job, SimpleNamespace(health=health), interval=60)
    await resumed.start(resume=True)
    await resumed.finish("completed")
    resumed.require_complete()
    health.assert_not_awaited()
    assert store.load_job(job.id).memory_usage == completed


@pytest.mark.asyncio
async def test_completed_measurements_cannot_be_reused_after_worker_identity_changes(job):
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(return_value=stats(32))), interval=60)
    await sampler.start()
    await sampler.finish("completed")
    changed = store.load_job(job.id)
    changed.worker_url = "http://different-worker.test:8188"
    store.save_job(changed)
    health = AsyncMock()
    resumed = H3MemorySampler(changed, SimpleNamespace(health=health), interval=60)
    with pytest.raises(ComfyError, match="measurements belong to a different worker"):
        await resumed.start(resume=True)
    health.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["missing_final_sample", "prior_error", "missing_finished_at", "mismatched_sample_count"])
async def test_resume_requires_complete_matching_terminal_evidence_before_reuse(job, incomplete):
    sampler = H3MemorySampler(job, SimpleNamespace(health=AsyncMock(return_value=stats(32))), interval=60)
    await sampler.start()
    await sampler.finish("completed")
    changed = store.load_job(job.id)
    if incomplete == "missing_final_sample":
        changed.memory_usage["samples"][-1]["phase"] = "execution"
    elif incomplete == "prior_error":
        changed.memory_usage["errors"].append({"error": "Earlier interval was missed"})
    elif incomplete == "missing_finished_at":
        changed.memory_usage["finished_at"] = None
    else:
        changed.memory_usage["sample_count"] += 1
    store.save_job(changed)
    resumed = H3MemorySampler(changed, SimpleNamespace(health=AsyncMock(return_value=stats(20))), interval=60)
    await resumed.start(resume=True)
    await resumed.finish("completed")
    final = store.load_job(job.id).memory_usage
    assert final["status"] == "incomplete"
    assert any("unobserved interval" in error["error"] for error in final["errors"])
