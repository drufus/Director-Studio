"""No pipeline may substitute a different remote upload for a declared input."""

import io

from PIL import Image
import pytest

from app.core.schemas import JobRecord, JobStatus
from app.pipelines.actor.pipeline import ActorPipeline
from app.pipelines.actor import workflow as actor_workflow
from app.pipelines.h3_ref2va.pipeline import H3Ref2VaPipeline
from app.pipelines.ref_frame.pipeline import RefFramePipeline


def job(params=None):
    return JobRecord(
        id="upload-test", status=JobStatus.queued, name="test", params=params or {},
        created_at="2026-09-08T00:00:00Z", updated_at="2026-09-08T00:00:00Z",
    )


@pytest.mark.parametrize("inputs", [{}, {"actor": ("actor.png", b"actor")}, {"wardrobe": ("wardrobe.png", b"wardrobe")}])
def test_actor_uploads_one_pixel_placeholder_and_uses_worker_returned_name(inputs):
    pipeline = ActorPipeline()
    current = job({"description": "An actor in a wool coat"})
    prepared = pipeline.prepare_upload_inputs(current, inputs)
    assert "__actor_blank" not in inputs
    filename, data = prepared["__actor_blank"]
    assert filename == actor_workflow.BLANK_IMAGE
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1, 1)
        assert image.format == "PPM"
    uploaded = {key: f"jobs/{current.id}/{name}" for key, (name, _) in prepared.items()}
    prompt, _ = pipeline.build_prompt(current, uploaded_images=uploaded)
    assert prompt[actor_workflow.NODE_ACTOR_IMAGE]["inputs"]["image"] == uploaded.get("actor", uploaded["__actor_blank"])
    assert prompt[actor_workflow.NODE_WARDROBE_IMAGE]["inputs"]["image"] == uploaded.get("wardrobe", uploaded["__actor_blank"])


def test_actor_placeholder_does_not_count_as_real_actor_reference():
    with pytest.raises(ValueError, match="description is required"):
        ActorPipeline().build_prompt(job(), uploaded_images={"__actor_blank": "remote-blank.ppm"})


def test_actor_cannot_assume_blank_exists_on_remote_disk():
    with pytest.raises(ValueError, match="blank reference was not uploaded"):
        ActorPipeline().build_prompt(job({"description": "An actor"}), uploaded_images={})


def test_actor_with_both_references_needs_no_placeholder():
    inputs = {"actor": ("actor.png", b"actor"), "wardrobe": ("wardrobe.png", b"wardrobe")}
    assert ActorPipeline().prepare_upload_inputs(job(), inputs) == inputs


@pytest.mark.parametrize("params,key", [
    ({"has_actor_ref": True}, "actor"),
    ({"has_wardrobe_ref": True}, "wardrobe"),
    ({"mode": "reference"}, "actor"),
])
def test_missing_declared_actor_reference_cannot_turn_into_text_generation(params, key):
    current = job({"description": "An actor", **params})
    pipeline = ActorPipeline()
    with pytest.raises(ValueError, match=f"missing {key} reference"):
        pipeline.prepare_upload_inputs(current, {})
    with pytest.raises(ValueError, match=f"missing {key} reference"):
        pipeline.build_prompt(current, uploaded_images={"__actor_blank": "blank.ppm"})


@pytest.mark.parametrize("params,error", [
    ({"image_keys": ["missing"]}, "image_keys declares missing inputs"),
    ({"image_keys": []}, "at least one reference image"),
    ({"image_keys": "image"}, "image_keys must be a list"),
    ({"image_keys": [1]}, "image_keys must be a list"),
    ({"image_keys": ["image", "image"]}, "duplicate input keys"),
    ({"image_keys": ["image"], "audio_keys": ["missing"]}, "audio_keys declares missing inputs"),
    ({"image_keys": ["image"], "native_audio_key": "missing"}, "native_audio_key declares missing inputs"),
    ({"image_keys": ["image"], "audio_keys": ["image"]}, "both image and audio"),
    ({"image_keys": ["image"], "audio_names": ["on-shared-disk.wav"]}, "worker-local audio filenames"),
])
def test_h3_rejects_invalid_declared_media_instead_of_using_other_uploads(params, error):
    pipeline = H3Ref2VaPipeline()
    with pytest.raises(ValueError, match=error):
        pipeline.prepare_upload_inputs(job(params), {"image": ("image.png", b"image"), "audio": ("voice.wav", b"audio")})
    with pytest.raises(ValueError, match=error):
        pipeline._remote_input_keys(params, {"image": "remote-image.png", "audio": "remote-voice.wav"})


def test_legacy_h3_order_never_treats_declared_audio_as_picture():
    image_keys, audio_keys, _ = H3Ref2VaPipeline._remote_input_keys(
        {"audio_keys": ["audio"]}, {"audio": "voice.wav", "image": "image.png"}
    )
    assert image_keys == ["image"]
    assert audio_keys == ["audio"]


@pytest.mark.parametrize("filename,data,error", [("voice.wav", b"audio", "unsupported file type"), ("image.png", b"", "empty or is not bytes")])
def test_h3_image_keys_require_nonempty_image_files(filename, data, error):
    with pytest.raises(ValueError, match=error):
        H3Ref2VaPipeline().prepare_upload_inputs(job({"image_keys": ["image"]}), {"image": (filename, data)})


def test_layout_rejects_missing_explicit_reference_without_replacing_it():
    with pytest.raises(ValueError, match="image_keys declares missing inputs"):
        RefFramePipeline().build_prompt(
            job({"description": "An actor in a room", "image_keys": ["missing"]}),
            uploaded_images={"unrelated": "unrelated.png"},
        )
