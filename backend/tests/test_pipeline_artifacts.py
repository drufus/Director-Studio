"""A plausible partial ComfyUI result must never satisfy a pipeline contract."""

from copy import deepcopy

import pytest

from app.core.comfy.artifacts import (
    image_manifest, validate_history, validate_mapped_outputs, video_manifest,
)
from app.core.schemas import ComfyImageRef, JobRecord, JobStatus
from app.pipelines.actor.pipeline import ActorPipeline
from app.pipelines.actor.schemas import ActorJobResponse
from app.pipelines.h3_ref2va.pipeline import H3Ref2VaPipeline
from app.pipelines.h3_ref2va.schemas import H3Ref2VaJobResponse
from app.pipelines.prop.pipeline import PropPipeline
from app.pipelines.prop.schemas import PropJobResponse
from app.pipelines.ref_frame.pipeline import RefFramePipeline
from app.pipelines.scene.pipeline import ScenePipeline
from app.pipelines.scene.schemas import SceneJobResponse


STATUS = {"completed": True, "status_str": "success"}


def job(**kwargs):
    return JobRecord(
        id="artifact-test", status=JobStatus.queued, name="test",
        created_at="2026-09-08T00:00:00Z", updated_at="2026-09-08T00:00:00Z", **kwargs,
    )


def media(filename):
    return {"filename": filename, "subfolder": "director-studio/test", "type": "output"}


def test_actor_manifest_requires_all_five_designated_outputs():
    pipeline = ActorPipeline()
    nodes = {"31": "master", "39": "bust_threeview", "46": "fullbody_threeview", "48": "asset_sheet", "57": "wardrobe_ref"}
    manifest = pipeline.expected_output_manifest(job(), {node: {} for node in nodes})
    history = {"status": STATUS, "outputs": {node: {"images": [media(f"{key}.png")]} for node, key in nodes.items()}}
    resolved = validate_history(manifest, history)
    validate_mapped_outputs(resolved, pipeline.map_history_outputs(history))
    for node in nodes:
        partial = deepcopy(history)
        del partial["outputs"][node]
        with pytest.raises(ValueError, match=f"Required output node {node}"):
            validate_history(manifest, partial)


@pytest.mark.parametrize("pipeline", [PropPipeline(), RefFramePipeline()])
def test_unrelated_preview_cannot_replace_designated_saver(pipeline):
    manifest = pipeline.expected_output_manifest(job(), {"13": {}})
    history = {"status": STATUS, "outputs": {"999": {"images": [media("preview.png")]}}}
    # The shipped mapper's legacy fallback is deliberately kept in workflow.py.
    assert pipeline.map_history_outputs(history)
    with pytest.raises(ValueError, match="Required output node 13"):
        validate_history(manifest, history)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_scene_requires_exact_requested_view_count(count):
    manifest = ScenePipeline().expected_output_manifest(
        job(params={"output_stems": ["front", "rear"]}), {"9": {}}
    )
    with pytest.raises(ValueError, match=f"expected 2 images; received {count}"):
        validate_history(manifest, {"status": STATUS, "outputs": {"9": {"images": [media(f"{i}.png") for i in range(count)]}}})


@pytest.mark.parametrize("change", [
    {"filename": ""}, {"filename": "../unsafe.png"}, {"subfolder": "../outside"},
    {"filename": "folder\\unsafe.png"}, {"subfolder": None}, {"type": "unknown"}, {"type": []},
])
def test_malformed_media_is_rejected_before_mapper(change):
    manifest = image_manifest({"13": {}}, {"13": ["master"]})
    with pytest.raises(ValueError):
        validate_history(manifest, {"status": STATUS, "outputs": {"13": {"images": [{**media("image.png"), **change}]}}})


@pytest.mark.parametrize("state", [
    {"node_errors": {"13": {"error": "failed"}}},
    {"status": {"completed": False, "status_str": "success"}},
    {"status": {"completed": True, "status_str": "error"}},
    {"status": {"completed": True, "messages": [["execution_interrupted", {}]]}},
])
def test_partial_outputs_with_execution_failure_are_rejected(state):
    manifest = image_manifest({"13": {}}, {"13": ["master"]})
    with pytest.raises(ValueError, match="history reports"):
        validate_history(manifest, {"status": STATUS, **state, "outputs": {"13": {"images": [media("partial.png")]}}})


def test_output_files_without_completion_status_are_not_success():
    manifest = image_manifest({"13": {}}, {"13": ["master"]})
    with pytest.raises(ValueError, match="no valid completion status"):
        validate_history(manifest, {"outputs": {"13": {"images": [media("partial.png")]}}})


def test_h3_selected_artifact_and_test_candidates_use_only_selected_terminal():
    history = {"status": STATUS, "outputs": {
        "999": {"videos": [media("unrelated.mp4")]},
        "214": {"gifs": [media("first.mp4"), media("second.mp4")]},
    }}
    manifest = video_manifest({"214": {}}, node_id="214", artifact_index=1, candidates=False)
    resolved = validate_history(manifest, history)
    validate_mapped_outputs(resolved, {"video": ComfyImageRef(**media("second.mp4"))})
    with pytest.raises(ValueError, match="designated node artifact"):
        validate_mapped_outputs(resolved, {"video": ComfyImageRef(**media("unrelated.mp4"))})

    test_manifest = video_manifest({"214": {}}, node_id="214", artifact_index=None, candidates=True)
    test_resolved = validate_history(test_manifest, history)
    assert test_manifest["logical_keys"] is None
    assert test_resolved["logical_keys"] == ["video_candidate_0", "video_candidate_1"]
    with pytest.raises(ValueError, match="missing=.*video_candidate_1"):
        validate_mapped_outputs(test_resolved, {"video_candidate_0": ComfyImageRef(**media("first.mp4"))})


@pytest.mark.parametrize("index,error", [(None, "without a selected artifact"), (2, "missing selected video artifact 2")])
def test_h3_ambiguous_or_missing_selected_artifact_fails(index, error):
    manifest = video_manifest({"92": {}}, node_id="92", artifact_index=index, candidates=False)
    with pytest.raises(ValueError, match=error):
        validate_history(manifest, {"status": STATUS, "outputs": {"92": {"videos": [media("a.mp4"), media("b.mp4")]}}})


def test_pipeline_h3_manifest_reads_job_snapshot_selection(monkeypatch):
    from types import SimpleNamespace

    pipeline = H3Ref2VaPipeline()
    monkeypatch.setattr(pipeline, "_profile_for_job", lambda current: SimpleNamespace(
        mapping=SimpleNamespace(output=SimpleNamespace(node_id="214", artifact_index=1))
    ))
    manifest = pipeline.expected_output_manifest(job(), {"214": {}, "92": {}})
    assert manifest["nodes"][0]["node_id"] == "214"
    assert manifest["nodes"][0]["artifact_index"] == 1


@pytest.mark.parametrize("response_type", [ActorJobResponse, SceneJobResponse, PropJobResponse, H3Ref2VaJobResponse])
def test_every_job_projection_preserves_worker_and_admission_evidence(response_type):
    fields = {
        "worker_id": "beastviii", "worker_url": "http://worker:8188",
        "worker_selected_at": "2026-09-08T00:00:01Z",
        "worker_selection": {"strategy": "least_queued", "considered": ["beastviii"]},
        "memory_admission": {"accepted": False, "ram_free_bytes": 100},
        "expected_artifacts": {"version": 1, "nodes": [{"node_id": "31"}]},
    }
    projected = response_type.from_job(job(**fields)).model_dump()
    assert {key: projected[key] for key in fields} == fields
