"""Submission-time artifact contracts and strict remote history validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..schemas import ComfyImageRef

_VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
_IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif",
)


def declared_input_keys(
    field: str, value: Any, inputs: Mapping[str, Any]
) -> list[str]:
    """Resolve explicit logical keys without substituting other uploaded files."""
    if not isinstance(value, list) or any(
        not isinstance(key, str) or not key.strip() for key in value
    ):
        raise ValueError(f"{field} must be a list of nonempty input key strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} contains duplicate input keys")
    missing = [key for key in value if key not in inputs or not inputs[key]]
    if missing:
        raise ValueError(f"{field} declares missing inputs: {', '.join(missing)}")
    return list(value)


def image_manifest(
    prompt: dict[str, Any], node_keys: dict[str, list[str]]
) -> dict[str, Any]:
    nodes = []
    logical_keys: list[str] = []
    for node_id, keys in node_keys.items():
        if node_id not in prompt:
            raise ValueError(f"Required image output node {node_id} is missing from prompt")
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError(f"Required image output node {node_id} has invalid logical keys")
        nodes.append({
            "node_id": node_id,
            "media": "image",
            "count": len(keys),
            "keys": list(keys),
        })
        logical_keys.extend(keys)
    if len(set(logical_keys)) != len(logical_keys):
        raise ValueError("Expected artifact logical keys must be unique")
    return {"version": 1, "nodes": nodes, "logical_keys": logical_keys}


def video_manifest(
    prompt: dict[str, Any], *, node_id: str, artifact_index: int | None, candidates: bool
) -> dict[str, Any]:
    if node_id not in prompt:
        raise ValueError(f"Required video output node {node_id} is missing from prompt")
    node = {
        "node_id": node_id, "media": "video", "min_count": 1,
        "artifact_index": artifact_index, "candidates": candidates,
    }
    return {
        "version": 1, "nodes": [node],
        "logical_keys": None if candidates else ["video"],
        "logical_key_prefix": "video_candidate_" if candidates else None,
    }


def _reference(value: Any, *, node_id: str) -> ComfyImageRef:
    if not isinstance(value, dict):
        raise ValueError(f"Output node {node_id} returned a non-object media reference")
    filename = value.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError(f"Output node {node_id} returned an empty or invalid filename")
    if (
        "\\" in filename
        or PurePosixPath(filename).name != filename
        or filename in {".", ".."}
    ):
        raise ValueError(f"Output node {node_id} returned an unsafe filename")
    subfolder = value.get("subfolder", "")
    if not isinstance(subfolder, str) or "\\" in subfolder:
        raise ValueError(f"Output node {node_id} returned an invalid subfolder")
    folder = PurePosixPath(subfolder)
    if folder.is_absolute() or ".." in folder.parts:
        raise ValueError(f"Output node {node_id} returned an unsafe subfolder")
    media_type = value.get("type", "output")
    if not isinstance(media_type, str) or media_type not in {"input", "output", "temp"}:
        raise ValueError(f"Output node {node_id} returned an invalid media type")
    return ComfyImageRef(filename=filename, subfolder=subfolder, type=media_type)


def validate_history(manifest: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    """Validate designated outputs, returning their exact logical reference mapping.

    The original persisted manifest remains the expectation declared before POST.
    Test workflows whose artifact count is only known after rendering get exact
    candidate keys in this returned copy for the mapper check.
    """
    if manifest.get("version") != 1 or not manifest.get("nodes"):
        raise ValueError("Job has no supported expected artifact manifest")
    if history.get("node_errors"):
        raise ValueError("ComfyUI history reports node errors; partial outputs are not complete")
    status = history.get("status")
    if not isinstance(status, dict) or not status:
        raise ValueError("ComfyUI history has no valid completion status")
    if status.get("completed") is not True:
        raise ValueError("ComfyUI history reports an incomplete execution")
    status_name = status.get("status_str")
    if status_name != "success":
        raise ValueError(f"ComfyUI history reports execution status {status_name}")
    for message in status.get("messages") or []:
        if (
            isinstance(message, (list, tuple)) and message
            and message[0] in {"execution_error", "execution_interrupted"}
        ):
            raise ValueError(
                f"ComfyUI history reports {message[0]}; partial outputs are not complete"
            )
    outputs = history.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("ComfyUI history has no output object")
    references: dict[str, dict[str, str]] = {}
    for requirement in manifest["nodes"]:
        node_id = requirement["node_id"]
        output = outputs.get(node_id)
        if not isinstance(output, dict):
            raise ValueError(f"Required output node {node_id} is missing from ComfyUI history")
        media = requirement["media"]
        if media == "image":
            values = output.get("images")
            if not isinstance(values, list):
                raise ValueError(f"Required output node {node_id} has no image list")
            if len(values) != requirement["count"]:
                raise ValueError(
                    f"Output node {node_id} expected {requirement['count']} images; received {len(values)}"
                )
            refs = [_reference(value, node_id=node_id) for value in values]
            if any(not ref.filename.lower().endswith(_IMAGE_SUFFIXES) for ref in refs):
                raise ValueError(f"Output node {node_id} returned a non-image artifact")
            references.update({
                key: ref.model_dump()
                for key, ref in zip(requirement["keys"], refs, strict=True)
            })
        elif media == "video":
            refs = []
            for field, values in output.items():
                if field == "videos" and not isinstance(values, list):
                    raise ValueError(f"Output node {node_id} returned an invalid video list")
                if not isinstance(values, list):
                    continue
                for value in values:
                    filename = value.get("filename") if isinstance(value, dict) else None
                    if field != "videos" and not (
                        isinstance(filename, str) and filename.lower().endswith(_VIDEO_SUFFIXES)
                    ):
                        continue
                    ref = _reference(value, node_id=node_id)
                    if not ref.filename.lower().endswith(_VIDEO_SUFFIXES):
                        raise ValueError(f"Output node {node_id} returned a non-video artifact")
                    refs.append(ref)
            if not refs:
                raise ValueError(f"Required output node {node_id} returned no video artifacts")
            if requirement.get("candidates"):
                prefix = manifest["logical_key_prefix"]
                references.update({
                    f"{prefix}{index}": ref.model_dump()
                    for index, ref in enumerate(refs)
                })
            else:
                index = requirement.get("artifact_index")
                if index is None:
                    if len(refs) != 1:
                        raise ValueError(
                            f"Output node {node_id} returned {len(refs)} videos "
                            "without a selected artifact"
                        )
                    index = 0
                if index >= len(refs):
                    raise ValueError(
                        f"Output node {node_id} is missing selected video "
                        f"artifact {index}; received {len(refs)}"
                    )
                references["video"] = refs[index].model_dump()
        else:
            raise ValueError(f"Unsupported expected artifact media kind: {media}")
    resolved = deepcopy(manifest)
    resolved["logical_keys"] = list(references)
    resolved["references"] = references
    return resolved


def validate_mapped_outputs(
    manifest: dict[str, Any], mapped: dict[str, ComfyImageRef]
) -> None:
    expected = manifest.get("logical_keys")
    if not isinstance(expected, list) or not expected:
        raise ValueError("Expected artifact manifest has no resolved logical keys")
    missing = sorted(set(expected) - set(mapped))
    unexpected = sorted(set(mapped) - set(expected))
    if missing or unexpected:
        raise ValueError(
            f"Output mapping does not match manifest: "
            f"missing={missing}, unexpected={unexpected}"
        )
    for key, ref in mapped.items():
        if not isinstance(ref, ComfyImageRef):
            raise ValueError(f"Output {key} is not a ComfyUI media reference")
        _reference(ref.model_dump(), node_id=key)
        required = manifest.get("references", {}).get(key)
        if required is not None and ref.model_dump() != required:
            raise ValueError(f"Output {key} does not match its designated node artifact")
