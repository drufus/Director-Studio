"""Transport and scheduling behavior against isolated HTTP workers, never the cluster."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.core.comfy.client import ComfyClient, ComfyError, validate_base_url
from app.core.comfy.workers import WorkerRegistry, parse_workers
from app.core.jobs.store import create_job, load_job
from app.api import comfy_workers, director, health


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(settings, "comfy_min_free_vram_gib", 12)


def stats(ram=16, vram=24):
    return {"system": {"ram_free": ram * 1024**3, "ram_total": 128 * 1024**3},
            "devices": [{"index": 0, "name": "test GPU", "vram_free": vram * 1024**3, "vram_total": 128 * 1024**3}]}


def make_job(**params):
    return create_job(pipeline_id="h3_ref2va", asset_kind="productions", name="test", notes="", params=params, seed=42, fixed_seed=True)


def make_registry(handler):
    transport = httpx.MockTransport(handler)
    def factory(url, **kwargs):
        return ComfyClient(url, transport=transport, **kwargs)
    return WorkerRegistry(parse_workers("a=http://worker-a.test:8188,b=http://worker-b.test:8189"), client_factory=factory)


def healthy(request):
    if request.url.path == "/system_stats":
        return httpx.Response(200, json=stats())
    if request.url.path == "/queue":
        return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
    raise AssertionError(f"Unexpected request {request.method} {request.url}")


@pytest.mark.parametrize("endpoint", ["http://100.88.79.40:8188", "http://thebeastiii:8188/", "http://beastiii:8188", "ftp://worker.test", "http://user:secret@worker.test", "http://worker.test/a", "http://worker.test?key=secret", "http://worker.test#secret"])
def test_unsafe_or_forbidden_origin_is_rejected_without_network(endpoint):
    with pytest.raises(ComfyError):
        validate_base_url(endpoint)


@pytest.mark.parametrize("config", ["a=http://a.test,a=http://b.test", "a=http://a.test,b=http://a.test", "a=http://a.test,", "http://a.test", "a b=http://a.test"])
def test_registry_rejects_ambiguous_configuration(config):
    with pytest.raises(ComfyError):
        parse_workers(config)


@pytest.mark.asyncio
async def test_n_worker_selection_is_durable_and_does_not_move(jobs):
    registry = make_registry(healthy)
    first, second, third = make_job(), make_job(), make_job()
    a, b = await asyncio.gather(registry.bind_job(first), registry.bind_job(second))
    assert {a.worker_id, b.worker_id} == {"a", "b"}
    for job, client in ((first, a), (second, b)):
        stored = load_job(job.id)
        assert stored.worker_id == client.worker_id
        assert stored.worker_url == client.base_url
        assert stored.worker_selected_at
        assert len(stored.worker_selection["workers"]) == 2
    pinned = await registry.bind_job(first, allow_selection=False)
    assert pinned.base_url == a.base_url
    await registry.release_job(first.id)
    assert (await registry.bind_job(third)).worker_id == a.worker_id


@pytest.mark.asyncio
async def test_down_worker_reported_and_not_selected(jobs):
    def handler(request):
        if request.url.host == "worker-a.test":
            raise httpx.ConnectError("Connection refused", request=request)
        return healthy(request)
    registry = make_registry(handler)
    job = make_job()
    selected = await registry.bind_job(job)
    assert selected.worker_id == "b"
    states = await registry.status(refresh=False)
    assert states[0]["status"] == "down"
    assert "Connection refused" in states[0]["error"]
    assert "worker-a.test" in load_job(job.id).worker_selection["workers"][0]["error"]
    requested = make_job(worker_id="a")
    with pytest.raises(ComfyError, match="matching 'a'"):
        await registry.bind_job(requested)
    assert load_job(requested.id).worker_id is None


@pytest.mark.asyncio
async def test_pinned_worker_failure_never_contacts_another_backend(jobs):
    calls = []
    down = False
    def handler(request):
        calls.append((request.url.host, request.url.path))
        if down:
            raise httpx.ConnectError("lost worker", request=request)
        return healthy(request)
    registry = make_registry(handler)
    job = make_job(worker_id="a")
    await registry.bind_job(job)
    down = True
    calls.clear()
    client = await registry.bind_job(job, allow_selection=False)
    with pytest.raises(ComfyError, match="lost worker"):
        await client.get_history("prompt-1")
    assert calls == [("worker-a.test", "/history/prompt-1")]
    assert (await registry.status(refresh=False))[0]["status"] == "down"
    assert load_job(job.id).worker_id == "a"


@pytest.mark.asyncio
async def test_changed_worker_url_or_missing_pin_is_not_resumed(jobs):
    registry = make_registry(healthy)
    job = make_job()
    with pytest.raises(ComfyError, match="no durable worker pin"):
        await registry.bind_job(job, allow_selection=False)
    await registry.bind_job(job)
    job.worker_url = "http://old-host.test"
    with pytest.raises(ComfyError, match="will not be rerouted"):
        await registry.bind_job(job)


@pytest.mark.asyncio
async def test_no_configuration_is_an_explicit_error(jobs):
    with pytest.raises(ComfyError, match="No render workers configured"):
        await WorkerRegistry([]).bind_job(make_job())


@pytest.mark.asyncio
async def test_h3_admission_records_measured_numbers_and_thresholds(jobs):
    registry = make_registry(healthy)
    job = make_job()
    client = await registry.bind_job(job)
    await registry.check_h3_admission(job, client)
    admission = load_job(job.id).memory_admission
    assert admission["accepted"] is True
    assert admission["worker_id"] == job.worker_id
    assert admission["checked_at"]
    assert admission["ram_free_bytes"] == 16 * 1024**3
    assert admission["devices"][0]["vram_free_bytes"] == 24 * 1024**3
    assert admission["min_free_vram_bytes"] == 12 * 1024**3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["low_vram", "no_stats", "missing", "threshold_unset"])
async def test_h3_admission_fails_loudly_and_persists_snapshot(jobs, monkeypatch, mode):
    registry = make_registry(healthy)
    job = make_job()
    await registry.bind_job(job)
    payload = stats(ram=128, vram=2 if mode == "low_vram" else 24)
    if mode == "missing":
        payload["devices"][0].pop("vram_free")
    if mode == "threshold_unset":
        monkeypatch.setattr(settings, "comfy_min_free_vram_gib", None)
    def handler(request):
        assert request.method == "GET" and request.url.path == "/system_stats"
        if mode == "no_stats":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=payload)
    client = ComfyClient(job.worker_url, worker_id=job.worker_id, transport=httpx.MockTransport(handler))
    with pytest.raises(ComfyError, match="H3 admission failed") as error:
        await registry.check_h3_admission(job, client)
    result = load_job(job.id).memory_admission
    assert result["accepted"] is False
    assert result["error"] in str(error.value)
    if mode == "low_vram":
        assert "free=" in result["error"] and "required=" in result["error"]
    if mode == "threshold_unset":
        assert "DS_COMFY_MIN_FREE_VRAM_GIB" in result["error"]
        assert result["devices"][0]["vram_free_bytes"] is not None


@pytest.mark.asyncio
async def test_image_and_audio_upload_then_download_use_same_worker():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/upload/image":
            assert b'filename="voice.wav"' in request.content
            assert b'RIFFtest' in request.content
            return httpx.Response(200, json={"name": "voice.wav", "subfolder": "refs", "type": "input"})
        assert request.url.path == "/view"
        assert request.url.params["subfolder"] == "renders"
        return httpx.Response(200, content=b"video bytes")
    client = ComfyClient("http://worker.test:8188", worker_id="test", transport=httpx.MockTransport(handler))
    assert await client.upload_image(b"RIFFtest", "voice.wav") == "refs/voice.wav"
    assert await client.download_image("clip.mp4", subfolder="renders") == b"video bytes"
    assert {str(r.url.host) for r in requests} == {"worker.test"}


@pytest.mark.asyncio
async def test_partial_graph_submission_is_cancelled_and_never_success():
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "accepted-partial", "node_errors": {"57": {"errors": [{"message": "model missing"}]}}})
        return httpx.Response(200)
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ComfyError, match="model missing"):
        await client.queue_prompt({})
    assert calls[1:] == [("/queue", {"delete": ["accepted-partial"]}), ("/interrupt", {"prompt_id": "accepted-partial"})]


@pytest.mark.asyncio
async def test_failed_history_with_partial_outputs_never_completes():
    payload = {"p": {"outputs": {"9": {"images": [{"filename": "partial.png"}]}}, "status": {"completed": False, "status_str": "error", "messages": [["execution_error", {"node_id": "10", "exception_message": "OOM"}]]}}}
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(ComfyError, match="OOM"):
        await client.wait_for_completion("p", poll_interval=0, timeout=1)


@pytest.mark.asyncio
async def test_outputs_without_completed_status_do_not_succeed():
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"p": {"outputs": {}}})))
    with pytest.raises(ComfyError, match="Timed out"):
        await client.wait_for_completion("p", poll_interval=0.001, timeout=0.01)


@pytest.mark.asyncio
async def test_node_dependency_validation_is_read_only_and_specific():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/object_info"
        return httpx.Response(200, json={"Loader": {"input": {"required": {"model_name": [["served.safetensors"]]}}}})
    client = ComfyClient("http://worker.test", worker_id="test", transport=httpx.MockTransport(handler))
    with pytest.raises(ComfyError, match="class 'MiniMaxH3ReferenceToVideo' is not installed"):
        await client.validate_workflow({"136": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}}})
    with pytest.raises(ComfyError, match="model_name='missing.safetensors'"):
        await client.validate_workflow({"1": {"class_type": "Loader", "inputs": {"model_name": "missing.safetensors"}}})


@pytest.mark.asyncio
async def test_redirect_never_contacts_a_different_worker():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://100.88.79.40:8188"})
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ComfyError, match="HTTP 302"):
        await client.health()
    assert calls == ["http://worker.test/system_stats"]


@pytest.mark.asyncio
async def test_empty_download_and_unsafe_upload_response_fail():
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"")))
    with pytest.raises(ComfyError, match="empty"):
        await client.download_image("clip.mp4")
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"name": "../escape", "type": "input"})))
    with pytest.raises(ComfyError, match="unsafe artifact path"):
        await client.upload_image(b"abc", "ref.png")


@pytest.mark.asyncio
async def test_explicit_free_requires_worker_under_independent_policy(monkeypatch):
    monkeypatch.setattr(director, "get_orchestrator", lambda: SimpleNamespace(shared_gpu_enabled=False))
    with pytest.raises(HTTPException) as error:
        await director.free_comfy_models()
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_status_api_preserves_down_worker_and_queue_numbers(monkeypatch):
    def handler(request):
        if request.url.host == "worker-b.test":
            return httpx.Response(503)
        return healthy(request)
    registry = make_registry(handler)
    monkeypatch.setattr(comfy_workers, "get_worker_registry", lambda: registry)
    result = await comfy_workers.render_workers()
    assert result["workers"][0]["status"] == "up"
    assert result["workers"][1]["status"] == "down"
    assert result["workers"][1]["queued_jobs"] is None


@pytest.mark.parametrize("endpoint", ["http://[::ffff:100.88.79.40]:8188", "http://100.88.79.40.:8188", "http://thebeastiii.:8188", "http://thebeastiii.cluster.ts.net:8188"])
def test_forbidden_origin_aliases_are_rejected_offline(endpoint):
    with pytest.raises(ComfyError, match="forbidden"):
        ComfyClient(endpoint)


def test_equivalent_origins_are_duplicate_workers():
    with pytest.raises(ComfyError, match="duplicate"):
        parse_workers("a=http://WORKER.test.:80,b=http://worker.test")


@pytest.mark.asyncio
async def test_execution_errors_do_not_expose_inputs_or_runtime_keys(monkeypatch):
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("test-runtime-secret"))
    event = {"node_id": "7", "node_type": "RemoteAPI", "exception_type": "RemoteError",
        "exception_message": "Request failed with test-workflow-secret and test-runtime-secret",
        "current_inputs": {"api_key": "test-workflow-secret"}, "traceback": ["private traceback"]}
    payload = {"p": {"status": {"status_str": "error", "messages": [["execution_error", event]]}, "outputs": {}}}
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(ComfyError) as error:
        await client.wait_for_completion("p", timeout=1)
    message = str(error.value)
    assert "RemoteError" in message and '"node_id": "7"' in message
    for secret in ("test-runtime-secret", "test-workflow-secret", "private traceback", "current_inputs"):
        assert secret not in message


@pytest.mark.asyncio
async def test_node_errors_exclude_received_values_and_keep_accepted_id_on_cancel_failure():
    from app.core.comfy.client import ComfySubmissionError
    def handler(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "partial-id", "node_errors": {"7": {"errors": [{"type": "invalid_key", "message": "Provider authentication failed", "extra_info": {"input_name": "api_key", "received_value": "test-private-workflow-key"}}]}}})
        raise httpx.ConnectError("worker lost during cancellation", request=request)
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ComfySubmissionError) as error:
        await client.queue_prompt({})
    assert error.value.prompt_id == "partial-id"
    assert "Provider authentication failed" in str(error.value)
    assert "worker lost during cancellation" in str(error.value)
    assert "test-private-workflow-key" not in str(error.value)


@pytest.mark.asyncio
async def test_partial_submission_keeps_accepted_id_when_cleanup_is_interrupted():
    from app.core.comfy.client import ComfySubmissionError
    def handler(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "partial-interrupted", "node_errors": {"7": {"errors": [{"message": "Missing model"}]}}})
        raise asyncio.CancelledError
    client = ComfyClient("http://worker.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ComfySubmissionError, match="cleanup.*interrupted") as error:
        await client.queue_prompt({})
    assert error.value.prompt_id == "partial-interrupted"
    assert "Missing model" in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("ram", [0.01, None])
async def test_h3_low_or_unavailable_ram_never_blocks_sufficient_vram(jobs, monkeypatch, ram):
    registry = make_registry(healthy)
    job = make_job(h3_profile_test=True, frames=56)
    await registry.bind_job(job)
    payload = stats(ram=0, vram=32)
    payload["system"]["ram_free"] = ram * 1024**3 if ram is not None else None
    monkeypatch.setattr(settings, "comfy_memory_threshold_provisional", True)
    client = ComfyClient(job.worker_url, worker_id=job.worker_id,
                         transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    await registry.check_h3_admission(job, client)
    admission = load_job(job.id).memory_admission
    assert admission["accepted"] is True
    assert admission["metric"] == "vram_free"
    assert admission["threshold_provisional"] is True
    assert "min_free_ram_bytes" not in admission


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {},
    {"frames": 56},
    {"h3_profile_test": False, "frames": 56},
    {"h3_profile_test": "true", "frames": 56},
    {"h3_profile_test": True, "frames": 112},
    {"h3_profile_test": True, "frames": "56"},
    {"h3_profile_test": True, "frames": 56.0},
])
async def test_provisional_threshold_rejects_production_and_noncalibration_jobs_with_snapshot(jobs, monkeypatch, params):
    monkeypatch.setattr(settings, "comfy_memory_threshold_provisional", True)
    monkeypatch.setattr(settings, "comfy_min_free_vram_gib", 1)
    registry = make_registry(healthy)
    job = make_job(**params)
    client = await registry.bind_job(job)
    with pytest.raises(ComfyError, match="only 56-frame profile calibration tests.*production H3 is blocked"):
        await registry.check_h3_admission(job, client)
    snapshot = load_job(job.id).memory_admission
    assert snapshot["accepted"] is False
    assert snapshot["threshold_provisional"] is True
    assert snapshot["min_free_vram_bytes"] == 1024**3
    assert snapshot["devices"][0]["vram_free_bytes"] == 24 * 1024**3
    assert snapshot["ram_free_bytes"] == 16 * 1024**3
    assert "DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=false" in snapshot["error"]


@pytest.mark.asyncio
async def test_provisional_threshold_admits_actual_56_frame_profile_calibration(jobs, monkeypatch):
    monkeypatch.setattr(settings, "comfy_memory_threshold_provisional", True)
    monkeypatch.setattr(settings, "comfy_min_free_vram_gib", 1)
    registry = make_registry(healthy)
    job = make_job(h3_profile_test=True, frames=56)
    client = await registry.bind_job(job)
    await registry.check_h3_admission(job, client)
    snapshot = load_job(job.id).memory_admission
    assert snapshot["accepted"] is True
    assert snapshot["error"] is None
    assert snapshot["threshold_provisional"] is True
    assert snapshot["devices"][0]["vram_free_bytes"] == 24 * 1024**3


@pytest.mark.asyncio
async def test_final_operator_threshold_allows_production_h3(jobs, monkeypatch):
    monkeypatch.setattr(settings, "comfy_memory_threshold_provisional", False)
    registry = make_registry(healthy)
    job = make_job(frames=240)
    client = await registry.bind_job(job)
    await registry.check_h3_admission(job, client)
    snapshot = load_job(job.id).memory_admission
    assert snapshot["accepted"] is True
    assert snapshot["threshold_provisional"] is False
    assert snapshot["min_free_vram_bytes"] == 12 * 1024**3
