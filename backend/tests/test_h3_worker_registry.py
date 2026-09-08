"""Custom H3 work may run only on workers covered by its captured proof."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import settings
from app.core.comfy.client import ComfyClient, ComfyError
from app.core.comfy.workers import WorkerRegistry, parse_workers
from app.core.jobs.store import create_job, load_job, save_job


A = {"worker_id": "a", "worker_url": "http://worker-a.test:8188"}
B = {"worker_id": "b", "worker_url": "http://worker-b.test:8188"}


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")


def job(**params):
    return create_job(
        pipeline_id="h3_ref2va", asset_kind="productions", name="proof scheduling",
        params={"h3_profile_id": "custom-validated", **params}, seed=42, fixed_seed=True,
    )


def registry(requests, *, offline=None):
    def handler(request):
        requests.append((request.url.host, request.url.path))
        if request.url.host == offline:
            raise httpx.ConnectError("worker offline", request=request)
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": []})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        raise AssertionError(f"Unexpected worker request: {request.method} {request.url}")

    def factory(url, **kwargs):
        return ComfyClient(url, transport=httpx.MockTransport(handler), **kwargs)

    return WorkerRegistry(parse_workers(
        "a=http://worker-a.test:8188,b=http://worker-b.test:8188"
    ), client_factory=factory)


@pytest.mark.asyncio
async def test_healthy_worker_without_profile_evidence_cannot_receive_job():
    requests = []
    workers = registry(requests)
    first, second = job(h3_eligible_workers=[B]), job(h3_eligible_workers=[B])
    for pending in (first, second):
        selected = await workers.bind_job(pending)
        assert selected.worker_id == "b"
        assert load_job(pending.id).worker_url == B["worker_url"]
    assert {state["status"] for state in await workers.status(refresh=False)} == {"up"}


@pytest.mark.asyncio
async def test_n_validated_workers_share_jobs_and_keep_exact_durable_identity():
    workers = registry([])
    pending = [job(h3_eligible_workers=[A, B]) for _ in range(4)]
    selected = await asyncio.gather(*(workers.bind_job(item) for item in pending))
    assert [client.worker_id for client in selected].count("a") == 2
    assert [client.worker_id for client in selected].count("b") == 2
    for item, client in zip(pending, selected):
        stored = load_job(item.id)
        assert {"worker_id": stored.worker_id, "worker_url": stored.worker_url} in [A, B]
        assert stored.worker_selected_at
        assert (await workers.bind_job(item, allow_selection=False)).base_url == client.base_url


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [None, [], {}, "worker-a", [None], [{}], [{"worker_id": "a"}], [{"worker_id": 1, "worker_url": A["worker_url"]}]])
async def test_missing_or_malformed_custom_worker_proof_fails_before_network(evidence):
    requests = []
    pending = job(**({} if evidence is None else {"h3_eligible_workers": evidence}))
    with pytest.raises(ComfyError, match="worker (evidence|eligibility)"):
        await registry(requests).bind_job(pending)
    assert requests == []
    assert load_job(pending.id).worker_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [[B], [{"worker_id": "a", "worker_url": "http://old-worker-a.test:8188"}]])
async def test_submitted_pin_without_matching_validation_cannot_resume(evidence):
    pending = job(h3_eligible_workers=evidence)
    pending.worker_id, pending.worker_url = A["worker_id"], A["worker_url"]
    pending.comfy_prompt_id = "submitted-prompt"
    save_job(pending)
    requests = []
    with pytest.raises(ComfyError, match="pin has no matching validation/test evidence"):
        await registry(requests).bind_job(pending, allow_selection=False)
    assert requests == []
    assert load_job(pending.id).comfy_prompt_id == "submitted-prompt"


@pytest.mark.asyncio
async def test_worker_url_change_does_not_reuse_old_profile_validation():
    pending = job(h3_eligible_workers=[{
        "worker_id": "a", "worker_url": "http://old-worker-a.test:8188"
    }])
    with pytest.raises(ComfyError, match="No eligible render worker.*validation/test evidence"):
        await registry([]).bind_job(pending)
    assert load_job(pending.id).worker_id is None


@pytest.mark.asyncio
async def test_validated_worker_failure_reports_cause_and_never_uses_healthy_unvalidated_worker():
    pending = job(h3_eligible_workers=[A])
    workers = registry([], offline="worker-a.test")
    with pytest.raises(ComfyError, match="No eligible render worker.*worker offline"):
        await workers.bind_job(pending)
    states = {state["id"]: state for state in await workers.status(refresh=False)}
    assert states["a"]["status"] == "down"
    assert "worker-a.test" in states["a"]["error"]
    assert states["b"]["status"] == "up"
    assert load_job(pending.id).worker_id is None


@pytest.mark.asyncio
async def test_explicitly_requested_healthy_unvalidated_worker_is_rejected():
    pending = job(h3_eligible_workers=[A], **B)
    with pytest.raises(ComfyError, match="No eligible render worker matching 'b'"):
        await registry([]).bind_job(pending)
    assert load_job(pending.id).worker_id is None
