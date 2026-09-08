"""Worker identity must survive every H3 setup and execution boundary."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config import settings
from app.core.jobs.store import create_job, job_dir, save_job
from app.core.schemas import OutputSlot, JobStatus
from app.pipelines.h3_ref2va.pipeline import H3Ref2VaPipeline
from app.workflow_profiles.h3 import (
    H3ProfileStore,
    H3WorkerBinding,
    H3WorkflowProfile,
    ProfileChangedError,
    ProfileStateError,
    ProfileStorageError,
)
from test_h3_profile_store import WORKER, _mapping, _valid_workflow, worker_evidence

OTHER = H3WorkerBinding(worker_id="worker-b", worker_url="http://worker-b:8188")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_profiles_dir", tmp_path / "profiles")
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")
    return H3ProfileStore()


def ready_import(store, worker=WORKER):
    import_id = store.create_import(_valid_workflow())
    store.bind_import_worker(import_id, worker)
    store.save_import_output(import_id, "92")
    store.save_import_mapping(import_id, _mapping())
    workflow_hash, mapping_hash = store.import_identity(import_id)
    store.record_inspection(
        import_id, worker, workflow_sha256=workflow_hash, metadata_sha256="a" * 64
    )
    store.record_validation_success(
        import_id,
        worker=worker,
        workflow_sha256=workflow_hash,
        mapping_sha256=mapping_hash,
        report={"valid": True},
        comfy_payload={"valid": True, "metadata_sha256": "a" * 64},
    )
    return import_id


def successful_job(store, import_id, worker=WORKER):
    workflow_hash, mapping_hash = store.testable_import_identity(import_id)
    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="worker proof",
        params={
            "h3_profile_test": True,
            "h3_profile_import_id": import_id,
            "h3_profile_test_workflow_sha256": workflow_hash,
            "h3_profile_test_mapping_sha256": mapping_hash,
            "h3_profile_test_boundary_sha256": store.boundary_sha256(
                store.load_import_mapping(import_id)
            ),
            **worker.model_dump(mode="json"),
        },
        seed=42,
        fixed_seed=True,
    )
    store.snapshot_import_for_job(job)
    job.worker_id, job.worker_url = worker.worker_id, worker.worker_url
    output = job_dir(job.id) / "outputs" / "proof.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"test video artifact")
    job.outputs = {
        "video": OutputSlot(
            key="video",
            label="Video",
            filename=output.name,
            path=str(output),
            url=f"/api/files/jobs/{job.id}/outputs/{output.name}",
        )
    }
    job.status = JobStatus.succeeded
    save_job(job)
    return job


def record_success(store, import_id, job):
    return store.record_test_success(
        import_id,
        workflow_sha256=job.params["h3_profile_test_workflow_sha256"],
        mapping_sha256=job.params["h3_profile_test_mapping_sha256"],
        boundary_sha256=job.params["h3_profile_test_boundary_sha256"],
        job_id=job.id,
    )


def test_worker_binding_rejects_credentials_forbidden_or_noncanonical_urls():
    for url in (
        "http://user:secret@example.com",
        "http://100.88.79.40:8188",
        "http://worker-a:8188/",
    ):
        with pytest.raises(ValidationError):
            H3WorkerBinding(worker_id="worker", worker_url=url)


@pytest.mark.parametrize(
    "replacement",
    [
        None,
        OTHER,
        H3WorkerBinding(worker_id=WORKER.worker_id, worker_url="http://changed:8188"),
    ],
)
def test_rebinding_or_clearing_worker_invalidates_all_proof_durably(store, replacement):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    record_success(store, import_id, job)
    store.bind_import_worker(import_id, replacement)
    reloaded = H3ProfileStore()
    lifecycle = reloaded.import_lifecycle(import_id)
    assert lifecycle["worker"] == (
        replacement.model_dump(mode="json") if replacement else None
    )
    assert lifecycle["status"] == "mapped"
    assert lifecycle["validated_at"] is None
    assert lifecycle["test_job_id"] is None
    assert "worker changed" in lifecycle["invalidation_reason"]
    with pytest.raises(ProfileStorageError):
        reloaded.testable_import_identity(import_id)
    with pytest.raises(ProfileStorageError):
        reloaded.activate_import(import_id)


def test_inflight_inspection_or_validation_cannot_write_after_worker_changes(store):
    import_id = ready_import(store)
    workflow_hash, mapping_hash = store.import_identity(import_id)
    store.bind_import_worker(import_id, OTHER)
    with pytest.raises(ProfileStateError, match="worker changed"):
        store.record_inspection(
            import_id, WORKER, workflow_sha256=workflow_hash, metadata_sha256="a" * 64
        )
    with pytest.raises(ProfileStateError, match="worker changed"):
        store.record_validation_success(
            import_id,
            worker=WORKER,
            workflow_sha256=workflow_hash,
            mapping_sha256=mapping_hash,
            report={"valid": True},
            comfy_payload={"valid": True, "metadata_sha256": "a" * 64},
        )


def test_metadata_changes_require_reinspection_and_invalidate_validation(store):
    import_id = ready_import(store)
    workflow_hash, mapping_hash = store.import_identity(import_id)
    store.record_inspection(
        import_id, WORKER, workflow_sha256=workflow_hash, metadata_sha256="a" * 64
    )
    assert store.testable_import_identity(import_id) == (workflow_hash, mapping_hash)
    with pytest.raises(ProfileStateError, match="metadata changed"):
        store.record_validation_success(
            import_id,
            worker=WORKER,
            workflow_sha256=workflow_hash,
            mapping_sha256=mapping_hash,
            report={"valid": True},
            comfy_payload={"valid": True, "metadata_sha256": "b" * 64},
        )
    store.record_inspection(
        import_id, WORKER, workflow_sha256=workflow_hash, metadata_sha256="b" * 64
    )
    assert store.import_lifecycle(import_id)["status"] == "mapped"
    with pytest.raises(ProfileStateError, match="Successful validation"):
        store.testable_import_identity(import_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_id", None),
        ("worker_url", None),
        ("worker_id", "other"),
        ("worker_url", "http://changed:8188"),
    ],
)
def test_test_success_requires_exact_durable_worker_pin(store, field, value):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    setattr(job, field, value)
    save_job(job)
    with pytest.raises(ProfileChangedError, match="durable worker pin"):
        record_success(store, import_id, job)


def test_test_snapshot_requires_exact_requested_url(store):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    job.params.pop("worker_url")
    with pytest.raises(ProfileChangedError, match="requested worker"):
        store.snapshot_import_for_job(job)


def test_activation_rechecks_test_worker_and_artifact(store):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    record_success(store, import_id, job)
    job.worker_url = OTHER.worker_url
    save_job(job)
    with pytest.raises(ProfileChangedError, match="durable worker pin"):
        store.activate_import(import_id)


def test_two_tested_workers_merge_eligibility_without_granting_untested_workers(store):
    first = ready_import(store, WORKER)
    record_success(store, first, successful_job(store, first, WORKER))
    profile = store.activate_import(first)
    first_snapshot_job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="original",
        params={},
        seed=42,
        fixed_seed=True,
    )
    store.snapshot_for_job(first_snapshot_job)
    second = ready_import(store, OTHER)
    record_success(store, second, successful_job(store, second, OTHER))
    merged = store.activate_import(second)
    assert merged.id == profile.id
    assert merged.eligible_workers == (WORKER, OTHER)
    assert store.resolve_active().eligible_workers == (WORKER, OTHER)
    assert store.load_job_snapshot(first_snapshot_job.id).eligible_workers == (WORKER,)
    assert first_snapshot_job.params["h3_eligible_workers"] == [
        WORKER.model_dump(mode="json")
    ]
    unknown = H3WorkerBinding(
        worker_id="unverified", worker_url="http://unverified:8188"
    )
    assert unknown not in merged.eligible_workers


@pytest.mark.parametrize("section", ["validation", "inspection", "test"])
def test_installed_custom_evidence_missing_worker_fails_loudly(store, section):
    graph = _valid_workflow()
    workflow_hash = store._sha256(store._json_bytes(graph))
    profile = H3WorkflowProfile(
        id="custom", workflow_sha256=workflow_hash, mapping=_mapping(), status="active"
    )
    validation, test = worker_evidence(
        workflow_hash, store.mapping_sha256(profile.mapping)
    )
    target = (
        validation
        if section == "validation"
        else validation["inspection"]
        if section == "inspection"
        else test
    )
    target.pop("worker")
    with pytest.raises(ProfileStateError, match="worker-bound"):
        store.install_profile(
            profile, graph, validation_record=validation, test_record=test
        )


def test_snapshot_evidence_cannot_expand_worker_eligibility(store):
    import_id = ready_import(store)
    record_success(store, import_id, successful_job(store, import_id))
    store.activate_import(import_id)
    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="snapshot",
        params={},
        seed=42,
        fixed_seed=True,
    )
    store.snapshot_for_job(job)
    proof_path = job_dir(job.id) / "workflow_profile" / "worker_proofs.json"
    proofs = json.loads(proof_path.read_text())
    proofs["workers"].append(OTHER.model_dump(mode="json"))
    proof_path.write_text(json.dumps(proofs))
    with pytest.raises(ProfileStorageError, match="eligibility evidence"):
        store.load_job_snapshot(job.id)


@pytest.mark.parametrize(
    "status", [JobStatus.running, JobStatus.failed, JobStatus.succeeded]
)
def test_execution_without_snapshot_never_substitutes_active_profile(store, status):
    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="missing snapshot",
        params={},
        seed=42,
        fixed_seed=True,
    )
    job.status = status
    with pytest.raises(ProfileStorageError, match="original workflow snapshot"):
        store.snapshot_for_job(job)
    with pytest.raises(ValueError, match="original workflow snapshot"):
        H3Ref2VaPipeline._profile_for_job(job)


def test_deleted_selected_custom_profile_does_not_resolve_builtin(store):
    import_id = ready_import(store)
    record_success(store, import_id, successful_job(store, import_id))
    profile = store.activate_import(import_id)
    store.profile_path(profile.id).unlink()
    with pytest.raises(ProfileStateError, match=f"Selected H3 profile '{profile.id}'"):
        store.resolve_active()


def test_pending_test_survives_reload_even_when_preparation_fails(store):
    import_id = ready_import(store)
    workflow_hash, mapping_hash = store.testable_import_identity(import_id)
    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="pending test",
        params={
            "h3_profile_test": True,
            "h3_profile_import_id": import_id,
            "h3_profile_test_workflow_sha256": workflow_hash,
            "h3_profile_test_mapping_sha256": mapping_hash,
            "h3_profile_test_boundary_sha256": store.boundary_sha256(
                store.load_import_mapping(import_id)
            ),
            **WORKER.model_dump(mode="json"),
        },
        seed=42,
        fixed_seed=True,
    )
    store.record_test_submission(import_id, job)
    assert H3ProfileStore().import_lifecycle(import_id)["test_job_id"] == job.id
    job.status = JobStatus.failed
    job.error = "Snapshot preparation failed"
    save_job(job)
    lifecycle = H3ProfileStore().import_lifecycle(import_id)
    assert lifecycle["status"] == "validated"
    assert lifecycle["test_job_id"] == job.id
    assert lifecycle["test_status"] == "failed"
    with pytest.raises(ProfileStateError, match="successful test"):
        store.activate_import(import_id)


def test_pending_test_cannot_survive_changed_worker_or_job_identity(store):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    store.record_test_submission(import_id, job)
    job.params["h3_profile_import_id"] = "imp-" + "a" * 32
    save_job(job)
    lifecycle = store.import_lifecycle(import_id)
    assert lifecycle["test_job_id"] is None
    assert "does not match" in lifecycle["invalidation_reason"]


def test_awaiting_artifact_choice_survives_reload_with_authoritative_worker(store):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    output = job_dir(job.id) / "outputs" / "second.mp4"
    output.write_bytes(b"second output")
    job.outputs = {
        "video_candidate_0": job.outputs["video"],
        "video_candidate_1": OutputSlot(
            key="video_candidate_1",
            label="Video",
            filename=output.name,
            path=str(output),
            url=f"/api/files/jobs/{job.id}/outputs/{output.name}",
        ),
    }
    save_job(job)
    record = record_success(store, import_id, job)
    assert record["status"] == "awaiting_selection"
    lifecycle = H3ProfileStore().import_lifecycle(import_id)
    assert lifecycle["status"] == "validated"
    assert lifecycle["test_job_id"] == job.id


def revalidate(store, import_id, *, metadata_sha256="a" * 64):
    workflow_hash, mapping_hash = store.import_identity(import_id)
    store.record_inspection(
        import_id,
        WORKER,
        workflow_sha256=workflow_hash,
        metadata_sha256=metadata_sha256,
    )
    store.record_validation_success(
        import_id,
        worker=WORKER,
        workflow_sha256=workflow_hash,
        mapping_sha256=mapping_hash,
        report={"valid": True},
        comfy_payload={"valid": True, "metadata_sha256": metadata_sha256},
    )


@pytest.mark.parametrize(
    "change", ["metadata", "worker_round_trip", "clear_round_trip"]
)
def test_late_success_cannot_restore_proof_after_worker_evidence_changes(store, change):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    if change == "metadata":
        revalidate(store, import_id, metadata_sha256="b" * 64)
    else:
        store.bind_import_worker(
            import_id, OTHER if change == "worker_round_trip" else None
        )
        store.bind_import_worker(import_id, WORKER)
        revalidate(store, import_id)
    with pytest.raises(
        ProfileChangedError, match="earlier worker binding or metadata inspection"
    ):
        record_success(store, import_id, job)
    lifecycle = store.import_lifecycle(import_id)
    assert lifecycle["status"] == "validated"
    assert lifecycle["test_job_id"] is None
    with pytest.raises(ProfileStateError, match="successful test"):
        store.activate_import(import_id)
    assert store.load_import_workflow(import_id) == _valid_workflow()


def test_activation_rechecks_snapshot_metadata_even_if_old_test_record_is_restored(
    store,
):
    import_id = ready_import(store)
    job = successful_job(store, import_id)
    record_success(store, import_id, job)
    test_path = store.imports_dir / import_id / "test.json"
    stale_proof = test_path.read_bytes()
    revalidate(store, import_id, metadata_sha256="b" * 64)
    test_path.write_bytes(stale_proof)
    with pytest.raises(
        ProfileChangedError, match="earlier worker binding or metadata inspection"
    ):
        store.activate_import(import_id)


def test_test_intent_cannot_take_a_new_snapshot_after_worker_binding_round_trip(store):
    import_id = ready_import(store)
    workflow_hash, mapping_hash = store.testable_import_identity(import_id)
    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="unprepared intent",
        params={
            "h3_profile_test": True,
            "h3_profile_import_id": import_id,
            "h3_profile_test_workflow_sha256": workflow_hash,
            "h3_profile_test_mapping_sha256": mapping_hash,
            "h3_profile_test_boundary_sha256": store.boundary_sha256(
                store.load_import_mapping(import_id)
            ),
            **WORKER.model_dump(mode="json"),
        },
        seed=42,
        fixed_seed=True,
    )
    store.record_test_submission(import_id, job)
    store.bind_import_worker(import_id, OTHER)
    store.bind_import_worker(import_id, WORKER)
    revalidate(store, import_id)
    with pytest.raises(
        ProfileChangedError, match="earlier worker binding or metadata inspection"
    ):
        store.snapshot_import_for_job(job)
    assert not (job_dir(job.id) / "workflow_profile").exists()
