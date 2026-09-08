"""Durability and complete-result contracts for remote ComfyUI jobs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core.comfy.artifacts import image_manifest
from app.core.jobs import runner, store
from app.core.jobs.execution_adapters.comfy import ComfyExecutionAdapter, ComfyExecutionRuntime
from app.core.schemas import ComfyImageRef, JobStatus


@pytest.fixture
def job_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")


class Pipeline:
    id = "remote_test"
    execution_adapter_id = "comfy"
    generation_kind = "image"
    output_labels = {"front": "Front", "back": "Back"}

    def prepare_upload_inputs(self, job, inputs):
        return inputs

    def build_prompt(self, job, *, uploaded_images):
        self.uploaded_images = uploaded_images
        return {"1": {"class_type": "SaveImage", "inputs": {}}}, 42

    def expected_output_manifest(self, job, prompt):
        return image_manifest(prompt, {"1": ["front", "back"]})

    def map_history_outputs(self, history, *, job):
        return {
            name: ComfyImageRef(**reference)
            for name, reference in zip(("front", "back"), history["outputs"]["1"]["images"])
        }

    def postprocess_job_outputs(self, job, saved):
        pass

    def on_job_succeeded(self, job):
        assert store.load_job(job.id).status == JobStatus.succeeded


class Client:
    def __init__(self, job_id):
        self.job_id = job_id
        self.uploads = []
        self.submissions = []
        self.downloads = []
        self.waits = []
        self.cancellations = []
        self.history = {
            "status": {"completed": True, "status_str": "success", "messages": []},
            "outputs": {"1": {"images": [
                {"filename": "front.png", "subfolder": "renders", "type": "output"},
                {"filename": "back.png", "subfolder": "renders", "type": "output"},
            ]}},
        }

    async def upload_image(self, data, filename):
        durable = store.load_job(self.job_id)
        assert durable.worker_id == "selected-worker"
        assert durable.worker_url == "http://selected-worker.invalid:8188"
        assert durable.worker_selected_at
        self.uploads.append((filename, data))
        return "input/" + filename

    async def queue_prompt(self, prompt):
        durable = store.load_job(self.job_id)
        assert durable.status == JobStatus.running
        assert durable.expected_artifacts["logical_keys"] == ["front", "back"]
        self.submissions.append(prompt)
        return "prompt-on-selected-worker"

    async def wait_for_completion(self, prompt_id, *, cancel_event):
        self.waits.append(prompt_id)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        return self.history

    async def download_image(self, filename, **kwargs):
        self.downloads.append(filename)
        return b"artifact bytes"

    async def cancel_prompt(self, prompt_id):
        self.cancellations.append(prompt_id)


def new_job(*, pipeline_id="remote_test"):
    return store.create_job(pipeline_id=pipeline_id, asset_kind="tests", name="Remote execution")


def make_runtime(client):
    binds = []
    events = []

    async def bind(job, *, allow_selection):
        binds.append((job.id, allow_selection))
        if not job.worker_id:
            if not allow_selection:
                raise ValueError("Submitted job is missing its pinned worker")
            job.worker_id = "selected-worker"
            job.worker_url = "http://selected-worker.invalid:8188"
            job.worker_selected_at = "2026-09-08T00:00:00Z"
            job.worker_selection = {"strategy": "least_queued"}
            store.save_job(job)
        events.append("bind")
        return client

    async def prepare(job):
        assert store.load_job(job.id).worker_id == "selected-worker"
        events.append("prepare")

    async def admit(job, bound_client):
        assert bound_client is client
        job.memory_admission = {"worker_id": job.worker_id, "passed": True}
        store.save_job(job)
        events.append("admit")

    runtime = ComfyExecutionRuntime(
        bind_worker=bind,
        release_worker=AsyncMock(),
        admit_h3=admit,
        prepare=prepare,
        finish=AsyncMock(),
        update_phase=AsyncMock(),
        save_completed_outputs=runner._save_completed_outputs,
    )
    return runtime, binds, events


@pytest.mark.asyncio
async def test_pin_precedes_upload_and_manifest_precedes_submit(job_root):
    job = new_job()
    client = Client(job.id)
    runtime, binds, events = make_runtime(client)
    pipeline = Pipeline()

    await ComfyExecutionAdapter().run(
        job, pipeline, {"reference": ("source.png", b"reference")}, asyncio.Event(), runtime
    )

    final = store.load_job(job.id)
    assert final.status == JobStatus.succeeded
    assert final.worker_id == "selected-worker"
    assert final.comfy_prompt_id == "prompt-on-selected-worker"
    assert binds == [(job.id, True)]
    assert events == ["bind", "prepare"]
    assert client.uploads == [(f"ds_{job.id}_reference.png", b"reference")]
    assert pipeline.uploaded_images == {"reference": f"input/ds_{job.id}_reference.png"}
    assert client.downloads == ["front.png", "back.png"]
    assert set(final.outputs) == {"front", "back"}
    runtime.release_worker.assert_awaited_once_with(job.id)


@pytest.mark.asyncio
async def test_h3_memory_admission_persisted_before_post(job_root):
    job = new_job(pipeline_id="h3_ref2va")
    client = Client(job.id)
    original_submit = client.queue_prompt

    async def checked_submit(prompt):
        assert store.load_job(job.id).memory_admission["passed"] is True
        return await original_submit(prompt)

    client.queue_prompt = checked_submit
    runtime, _, events = make_runtime(client)
    await ComfyExecutionAdapter().run(job, Pipeline(), {}, asyncio.Event(), runtime)
    assert store.load_job(job.id).status == JobStatus.succeeded
    assert events == ["bind", "prepare", "admit"]


@pytest.mark.asyncio
async def test_failed_memory_admission_never_posts(job_root):
    from dataclasses import replace

    job = new_job(pipeline_id="h3_ref2va")
    client = Client(job.id)
    runtime, _, _ = make_runtime(client)
    runtime = replace(runtime, admit_h3=AsyncMock(side_effect=ValueError("RAM free 2 GiB < required 16 GiB")))
    await ComfyExecutionAdapter().run(job, Pipeline(), {}, asyncio.Event(), runtime)
    final = store.load_job(job.id)
    assert final.status == JobStatus.failed
    assert "RAM free 2 GiB < required 16 GiB" in final.error
    assert client.submissions == []
    assert final.worker_id == "selected-worker"


@pytest.mark.asyncio
async def test_resume_uses_pin_without_upload_submit_or_memory_admission(job_root):
    job = new_job()
    client = Client(job.id)
    runtime, binds, events = make_runtime(client)
    await runtime.bind_worker(job, allow_selection=True)
    job.comfy_prompt_id = "already-submitted"
    job.status = JobStatus.running
    job.expected_artifacts = Pipeline().expected_output_manifest(job, {"1": {}})
    store.save_job(job)
    binds.clear()
    events.clear()

    await ComfyExecutionAdapter().resume(job, Pipeline(), asyncio.Event(), runtime)

    assert store.load_job(job.id).status == JobStatus.succeeded
    assert binds == [(job.id, False)]
    assert client.uploads == []
    assert client.submissions == []
    assert client.waits == ["already-submitted"]
    assert events == ["bind", "prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["worker", "manifest"])
async def test_resume_missing_durable_binding_or_manifest_fails_without_selection(job_root, missing):
    job = new_job()
    job.status = JobStatus.running
    job.comfy_prompt_id = "already-submitted"
    if missing != "worker":
        job.worker_id = "selected-worker"
        job.worker_url = "http://selected-worker.invalid:8188"
    if missing != "manifest":
        job.expected_artifacts = Pipeline().expected_output_manifest(job, {"1": {}})
    store.save_job(job)
    client = Client(job.id)
    runtime, binds, _ = make_runtime(client)

    await ComfyExecutionAdapter().resume(job, Pipeline(), asyncio.Event(), runtime)

    final = store.load_job(job.id)
    assert final.status == JobStatus.failed
    assert missing in final.error
    assert not any(allow for _, allow in binds)
    assert client.waits == []
    assert client.submissions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_node", "partial_outputs", "node_error", "failed_status", "incomplete_status", "empty_download", "partial_download", "mapping_drops_output", "postprocess", "postprocess_drops_output", "postprocess_empty_output", "success_hook", "terminal_sync"])
async def test_completion_failure_is_never_success(job_root, monkeypatch, defect):
    job = new_job()
    pipeline = Pipeline()
    client = Client(job.id)
    runtime, _, _ = make_runtime(client)
    if defect == "missing_node":
        client.history["outputs"] = {}
    elif defect == "partial_outputs":
        client.history["outputs"]["1"]["images"].pop()
    elif defect == "node_error":
        client.history["node_errors"] = {"22": {"errors": [{"message": "sampler failed"}]}}
    elif defect == "failed_status":
        client.history["status"]["status_str"] = "error"
    elif defect == "incomplete_status":
        client.history["status"]["completed"] = False
    elif defect == "empty_download":
        client.download_image = AsyncMock(return_value=b"")
    elif defect == "partial_download":
        client.download_image = AsyncMock(side_effect=[b"first artifact", ConnectionError("worker disconnected")])
    elif defect == "mapping_drops_output":
        original_map = pipeline.map_history_outputs
        pipeline.map_history_outputs = lambda history, *, job: {"front": original_map(history, job=job)["front"]}
    elif defect == "postprocess":
        def fail_postprocess(job, saved):
            raise ValueError("video metadata write failed")
        pipeline.postprocess_job_outputs = fail_postprocess
    elif defect == "postprocess_drops_output":
        pipeline.postprocess_job_outputs = lambda job, saved: saved.pop("back")
    elif defect == "postprocess_empty_output":
        pipeline.postprocess_job_outputs = lambda job, saved: saved["back"].write_bytes(b"")
    elif defect == "success_hook":
        def fail_hook(job):
            raise ValueError("validation evidence write failed")
        pipeline.on_job_succeeded = fail_hook
    elif defect == "terminal_sync":
        def fail_sync(job):
            raise ValueError("shot store is read-only")
        monkeypatch.setattr("app.core.jobs.shot_sync.on_pipeline_job_terminal", fail_sync)

    await ComfyExecutionAdapter().run(job, pipeline, {}, asyncio.Event(), runtime)

    final = store.load_job(job.id)
    assert final.status == JobStatus.failed, final.model_dump()
    assert final.error
    assert final.comfy_prompt_id == "prompt-on-selected-worker"
    runtime.release_worker.assert_awaited_once_with(job.id)
    if defect in {"missing_node", "partial_outputs", "node_error", "failed_status", "incomplete_status", "mapping_drops_output"}:
        assert client.downloads == []


@pytest.mark.asyncio
async def test_error_text_containing_cancel_does_not_imply_user_cancellation(job_root):
    job = new_job()
    client = Client(job.id)
    client.wait_for_completion = AsyncMock(side_effect=RuntimeError("could not cancel unrelated prompt"))
    runtime, _, _ = make_runtime(client)
    await ComfyExecutionAdapter().run(job, Pipeline(), {}, asyncio.Event(), runtime)
    final = store.load_job(job.id)
    assert final.status == JobStatus.failed
    assert final.error == "could not cancel unrelated prompt"


@pytest.mark.asyncio
async def test_cancel_targets_only_recorded_worker_prompt(job_root):
    job = new_job()
    client = Client(job.id)
    runtime, binds, _ = make_runtime(client)
    await runtime.bind_worker(job, allow_selection=True)
    job.comfy_prompt_id = "owned-prompt"
    store.save_job(job)
    binds.clear()

    await ComfyExecutionAdapter().cancel(job, runtime)

    assert binds == [(job.id, False)]
    assert client.cancellations == ["owned-prompt"]


@pytest.mark.asyncio
async def test_cancel_without_prompt_never_contacts_any_worker(job_root):
    job = new_job()
    runtime, binds, _ = make_runtime(Client(job.id))
    await ComfyExecutionAdapter().cancel(job, runtime)
    assert binds == []


@pytest.mark.asyncio
async def test_running_job_without_id_is_ambiguous_and_never_replayed(job_root, monkeypatch):
    job = new_job()
    job.status = JobStatus.running
    job.worker_id = "selected-worker"
    job.worker_url = "http://selected-worker.invalid:8188"
    store.save_job(job)
    monkeypatch.setattr(runner, "get_pipeline", lambda _id: Pipeline())
    start = AsyncMock()
    monkeypatch.setattr(runner, "start_pipeline_job", start)

    assert await runner.recover_interrupted_jobs() == []
    final = store.load_job(job.id)
    assert final.status == JobStatus.failed
    assert "Submission may already have occurred" in final.error
    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.uploading])
async def test_only_presubmit_states_replay_saved_inputs(job_root, monkeypatch, status):
    job = new_job()
    job.status = status
    job.worker_id = "selected-worker"
    job.worker_url = "http://selected-worker.invalid:8188"
    store.save_job(job)
    store.save_input_file(job.id, "voice", "voice.wav", b"audio")
    monkeypatch.setattr(runner, "get_pipeline", lambda _id: Pipeline())
    start = AsyncMock()
    monkeypatch.setattr(runner, "start_pipeline_job", start)

    assert await runner.recover_interrupted_jobs() == [job.id]
    replayed = start.await_args.args[0]
    assert replayed.worker_id == "selected-worker"
    assert replayed.worker_url == "http://selected-worker.invalid:8188"
    assert start.await_args.kwargs["images"] == {"voice": ("voice.wav", b"audio")}


def test_atomic_job_write_failure_preserves_complete_prior_record(job_root, monkeypatch):
    job = new_job()
    original = store.load_job(job.id).model_dump()
    job.worker_id = "must-not-half-persist"

    def failed_replace(source, destination):
        # Readers still get the old complete JSON while the replacement exists.
        assert store.load_job(job.id).model_dump() == original
        raise OSError("disk refused atomic rename")

    monkeypatch.setattr(store.os, "replace", failed_replace)
    with pytest.raises(OSError, match="atomic rename"):
        store.save_job(job)
    assert store.load_job(job.id).model_dump() == original
    assert list(store.job_dir(job.id).glob(".job-*.tmp")) == []


@pytest.mark.asyncio
async def test_process_task_interruption_preserves_submitted_job_for_recovery(job_root):
    job = new_job()
    client = Client(job.id)
    reached_wait = asyncio.Event()

    async def interrupted_wait(prompt_id, *, cancel_event):
        reached_wait.set()
        await asyncio.Future()

    client.wait_for_completion = interrupted_wait
    runtime, _, _ = make_runtime(client)
    task = asyncio.create_task(ComfyExecutionAdapter().run(job, Pipeline(), {}, asyncio.Event(), runtime))
    await reached_wait.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupted = store.load_job(job.id)
    assert interrupted.status == JobStatus.running
    assert interrupted.comfy_prompt_id == "prompt-on-selected-worker"
    assert interrupted.worker_id == "selected-worker"
    assert interrupted.expected_artifacts["logical_keys"] == ["front", "back"]
    runtime.finish.assert_not_awaited()
    runtime.release_worker.assert_awaited_once_with(job.id)


@pytest.mark.asyncio
async def test_registry_runs_two_jobs_on_distinct_workers_with_pinned_file_movement(job_root, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from app.core.comfy.workers import Worker, WorkerRegistry

    uploads = []
    submissions = []
    waits = []
    downloads = []
    both_uploading = asyncio.Event()

    class FleetPipeline(Pipeline):
        def build_prompt(self, job, *, uploaded_images):
            return {"1": {"class_type": "SaveImage", "inputs": {"job_id": job.id}}}, 42

    class FleetClient:
        def __init__(self, base_url, *, worker_id, on_failure):
            self.base_url = base_url
            self.worker_id = worker_id

        async def health(self):
            return {"system": {}}

        async def get_queue(self):
            return {"queue_running": [], "queue_pending": []}

        async def upload_image(self, data, filename):
            job_id = filename.removeprefix("ds_").removesuffix("_reference.png")
            durable = store.load_job(job_id)
            assert (durable.worker_id, durable.worker_url) == (self.worker_id, self.base_url)
            uploads.append((job_id, self.worker_id))
            if len(uploads) == 2:
                both_uploading.set()
            await asyncio.wait_for(both_uploading.wait(), timeout=2)
            return filename

        async def queue_prompt(self, prompt):
            job_id = prompt["1"]["inputs"]["job_id"]
            assert store.load_job(job_id).worker_id == self.worker_id
            submissions.append((job_id, self.worker_id))
            return f"{self.worker_id}-{job_id}"

        async def wait_for_completion(self, prompt_id, *, cancel_event):
            assert prompt_id.startswith(self.worker_id + "-")
            waits.append((prompt_id, self.worker_id))
            return Client("unused").history

        async def download_image(self, filename, **kwargs):
            downloads.append((filename, self.worker_id))
            return self.worker_id.encode()

    workers = [Worker(name, f"http://{name}.invalid:8188") for name in ("worker-a", "worker-b")]
    registry = WorkerRegistry(workers, client_factory=FleetClient)
    monkeypatch.setattr(runner, "get_worker_registry", lambda: registry)
    monkeypatch.setattr(runner, "get_pipeline", lambda _id: FleetPipeline())
    monkeypatch.setattr(runner, "get_orchestrator", lambda: SimpleNamespace(release_generation=AsyncMock()))
    monkeypatch.setattr(runner, "prepare_comfy", AsyncMock())
    monkeypatch.setattr(runner, "finish_comfy", AsyncMock())
    monkeypatch.setattr(runner, "update_generation_phase", AsyncMock())
    jobs = [new_job(), new_job()]

    await asyncio.gather(*(
        runner._run_job(job.id, {"reference": ("source.png", b"reference")}, asyncio.Event())
        for job in jobs
    ))

    finals = [store.load_job(job.id) for job in jobs]
    assert all(job.status == JobStatus.succeeded for job in finals)
    assert {job.worker_id for job in finals} == {"worker-a", "worker-b"}
    for job in finals:
        assert (job.id, job.worker_id) in uploads
        assert (job.id, job.worker_id) in submissions
        assert (job.comfy_prompt_id, job.worker_id) in waits
        assert ("front.png", job.worker_id) in downloads
        assert ("back.png", job.worker_id) in downloads
        assert Path(job.outputs["front"].path).read_bytes() == job.worker_id.encode()
    assert registry._claims == {}


@pytest.mark.asyncio
async def test_partial_submission_error_preserves_remote_prompt_identity(job_root):
    job = new_job()
    client = Client(job.id)

    class PartialSubmissionError(RuntimeError):
        prompt_id = "partially-accepted-prompt"

    client.queue_prompt = AsyncMock(side_effect=PartialSubmissionError(
        "node_errors: selected output is invalid; cancellation also failed on selected-worker"
    ))
    runtime, _, _ = make_runtime(client)
    await ComfyExecutionAdapter().run(job, Pipeline(), {}, asyncio.Event(), runtime)

    final = store.load_job(job.id)
    assert final.status == JobStatus.failed
    assert final.comfy_prompt_id == "partially-accepted-prompt"
    assert final.worker_id == "selected-worker"
    assert "cancellation also failed" in final.error
    assert client.waits == []
    assert client.downloads == []
