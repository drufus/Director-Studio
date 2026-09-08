"""Runtime setup API for portable H3 Ref2AV workflow profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from ..core.comfy.client import ComfyError, workflow_metadata_sha256, validate_base_url
from ..core.comfy.workers import get_worker_registry
from ..core.jobs import create_job, start_pipeline_job
from ..core.jobs import store as job_store
from ..core.schemas import JobStatus
from ..core.library.images import resolve_asset_image
from ..core.library.store import asset_dir, load_asset
from ..core.paths import LIBRARY_KINDS
from ..pipelines.h3_ref2va.workflow import fill_profile_graph
from ..workflow_profiles.h3 import (
    H3BoundaryMapping,
    H3WorkerBinding,
    H3ProfileStore,
    ProfileChangedError,
    ProfileStateError,
    ProfileStorageError,
    ResolvedH3Profile,
)
from ..workflow_profiles.h3.inspector import MAX_WORKFLOW_BYTES, inspect_h3_workflow
from ..workflow_profiles.h3.validator import validate_h3_contract

router = APIRouter(prefix="/workflow-profiles/h3", tags=["h3-workflow-profiles"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectProfileRequest(_StrictModel):
    profile_id: StrictStr = Field(pattern=r"[a-z0-9][a-z0-9-]{0,63}")


class SelectOutputRequest(_StrictModel):
    node_id: StrictStr = Field(min_length=1)


class TestProfileRequest(_StrictModel):
    worker_id: StrictStr = Field(min_length=1)
    worker_url: StrictStr = Field(min_length=1)
    picture_asset_id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    audio_asset_id: StrictStr | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )


class BindWorkerRequest(_StrictModel):
    worker_id: StrictStr | None
    worker_url: StrictStr | None

    @model_validator(mode="after")
    def paired_identity(self):
        if (self.worker_id is None) != (self.worker_url is None):
            raise ValueError("worker_id and worker_url must both be set or both be null")
        return self


class SelectTestOutputRequest(_StrictModel):
    artifact_index: int = Field(ge=0)


_TEST_PROMPT = """subject_definitions:
<Picture 1> defines the subject and visual identity for the whole clip.{audio_binding}
summary:
A neutral workflow setup test with natural, stable motion.
retention_analysis:
Preserve the subject identity, proportions, clothing, lighting, and background.
detailed_description:
The subject remains composed while the camera makes a slow camera push.
overall_soundscape:
Normal ambient audio at a natural level with no sudden or exaggerated sounds.
non_diegetic_music:
No music."""


def _resolve_picture_asset(asset_id: str) -> tuple[str, bytes] | None:
    role_for_kind = {
        "actors": "actor",
        "costumes": "costume",
        "scenes": "scene",
        "props": "prop",
        "layouts": "layout_ref_frame",
    }
    for kind in LIBRARY_KINDS:
        asset = load_asset(kind, asset_id)
        if asset is None:
            continue
        resolved = resolve_asset_image(asset, role=role_for_kind.get(kind, "other"))
        if resolved is not None:
            filename, data, _file_key = resolved
            return filename, data
    return None


def _resolve_voice_asset(asset_id: str) -> tuple[str, bytes] | None:
    asset = load_asset("voices", asset_id)
    if asset is None or not bool((asset.meta or {}).get("h3_ready")):
        return None
    filename = (asset.files or {}).get("reference")
    if not isinstance(filename, str) or Path(filename).name != filename:
        return None
    directory = asset_dir("voices", asset.id, project_id=asset.project_id)
    path = (directory / filename).resolve()
    try:
        path.relative_to(directory.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path.name, path.read_bytes()


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "details": details or {}},
    )


def _store_error(exc: ProfileStorageError) -> JSONResponse:
    if isinstance(exc, ProfileStateError):
        status = 422 if exc.code == "contract_validation_failed" else 409
        return _error(status, exc.code, str(exc), exc.details)
    if isinstance(exc, ProfileChangedError):
        return _error(409, "profile_changed", str(exc))
    message = str(exc)
    code = (
        "invalid_import_id"
        if message == "Invalid workflow import ID"
        else "profile_storage_error"
    )
    return _error(400, code, message)


def _active_payload(store: H3ProfileStore) -> dict[str, Any]:
    resolved = store.resolve_active()
    return {
        "profile_id": resolved.profile_id,
        "display_name": resolved.display_name,
        "source": resolved.source,
        "workflow_sha256": resolved.workflow_sha256,
        "contract_version": 2,
        "validated_at": resolved.validated_at,
        "eligible_workers": [worker.model_dump(mode="json") for worker in resolved.eligible_workers],
        "selection_source": resolved.selection_source,
        "selection_message": resolved.selection_message,
        "warning": (
            {
                "code": resolved.warning.code,
                "message": resolved.warning.message,
                "details": resolved.warning.details,
            }
            if resolved.warning
            else None
        ),
    }


@router.get("")
def list_h3_profiles() -> dict[str, Any]:
    store = H3ProfileStore()
    active_error = None
    selected_profile_id = None
    try:
        active = _active_payload(store)
        selected_profile_id = active["profile_id"]
    except ProfileStorageError as exc:
        active = None
        details = exc.details if isinstance(exc, ProfileStateError) else {}
        selected_profile_id = details.get("profile_id")
        active_error = {"code": exc.code if isinstance(exc, ProfileStateError) else "selected_profile_unavailable", "message": str(exc), "details": details}
    profiles: list[dict[str, Any]] = [
        {
            "profile_id": "builtin-official-h3",
            "display_name": "Built-in Official H3",
            "source": "builtin",
            "status": (
                "active"
                if active and active["profile_id"] == "builtin-official-h3"
                else "available"
            ),
            "workflow_sha256": store.resolve_builtin().workflow_sha256,
        }
    ]
    for profile in store.list_installed_profiles():
        try:
            resolved = store.resolve_profile(profile.id)
        except ProfileStorageError:
            continue
        profiles.append(
            {
                "profile_id": profile.id,
                "display_name": resolved.display_name,
                "source": "custom",
                "status": "active" if active and profile.id == active["profile_id"] else "tested",
                "eligible_workers": [worker.model_dump(mode="json") for worker in resolved.eligible_workers],
                "workflow_sha256": profile.workflow_sha256,
            }
        )
    return {"active": active, "active_error": active_error, "selected_profile_id": selected_profile_id, "profiles": profiles}


@router.post("/imports", status_code=201, response_model=None)
async def import_h3_workflow(
    workflow: Annotated[UploadFile, File()],
) -> dict[str, Any] | JSONResponse:
    raw = await workflow.read(MAX_WORKFLOW_BYTES + 1)
    if len(raw) > MAX_WORKFLOW_BYTES:
        return _error(
            400,
            "workflow_too_large",
            f"Workflow API JSON exceeds {MAX_WORKFLOW_BYTES // 1024 // 1024} MiB",
        )
    try:
        graph = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(
            400, "invalid_workflow_json", "Workflow must contain valid UTF-8 JSON"
        )
    if not isinstance(graph, dict):
        return _error(400, "invalid_workflow_json", "Workflow JSON must be an object")
    try:
        analysis = inspect_h3_workflow(graph)
        structural_issues = [
            issue
            for issue in analysis.issues
            if issue.code
            in {
                "invalid_structure",
                "invalid_node",
                "invalid_inputs",
                "invalid_class_type",
            }
        ]
        if not graph or structural_issues:
            return _error(
                400,
                "invalid_workflow",
                "Workflow must be a valid API graph",
                {
                    "issues": [
                        issue.model_dump(mode="json") for issue in structural_issues
                    ]
                },
            )
        store = H3ProfileStore()
        filename = Path(
            (workflow.filename or "Custom H3 workflow.json").replace("\\", "/")
        ).stem
        display_name = filename.removesuffix(".api")
        import_id = store.create_import(graph, display_name=display_name)
        workflow_sha256 = store.import_workflow_sha256(import_id)
    except (TypeError, ValueError) as exc:
        return _error(400, "invalid_workflow", str(exc))
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {
        "import_id": import_id,
        "workflow_sha256": workflow_sha256,
        "filename": workflow.filename or "workflow.api.json",
    }


def _configured_worker(worker_id: str, worker_url: str):
    """Resolve the exact browser-observed endpoint before any worker request."""
    try:
        expected_url = validate_base_url(worker_url)
        client = get_worker_registry().client_for(worker_id)
    except ComfyError as exc:
        raise ProfileStateError("worker_unavailable", str(exc), details={"worker_id": worker_id}) from exc
    if client.base_url != expected_url:
        raise ProfileStateError("worker_changed", f"Worker {worker_id!r} now points to a different endpoint. Select its current address and inspect again", details={"worker_id": worker_id, "expected_url": expected_url, "configured_url": client.base_url})
    return H3WorkerBinding(worker_id=worker_id, worker_url=client.base_url), client


def _bound_worker(store: H3ProfileStore, import_id: str, worker_id: str, worker_url: str):
    worker, client = _configured_worker(worker_id, worker_url)
    store.require_import_worker(import_id, worker)
    return worker, client


@router.put("/imports/{import_id:path}/worker", response_model=None)
def bind_h3_import_worker(import_id: str, body: BindWorkerRequest):
    store = H3ProfileStore()
    try:
        worker = None
        if body.worker_id is not None:
            worker, _client = _configured_worker(body.worker_id, body.worker_url)
        binding = store.bind_import_worker(import_id, worker)
        return {"worker": binding.model_dump(mode="json") if binding else None, "lifecycle": store.import_lifecycle(import_id)}
    except ProfileStorageError as exc:
        return _store_error(exc)


async def _inspect_import_metadata(store: H3ProfileStore, import_id: str, worker_id: str, worker_url: str):
    worker, client = _bound_worker(store, import_id, worker_id, worker_url)
    binding_generation = store.import_binding_generation(import_id)
    graph, workflow_hash = store.load_import_workflow_snapshot(import_id)
    try:
        object_info = await client.get_object_info()
    except ComfyError as exc:
        raise HTTPException(503, str(exc)) from None
    _bound_worker(store, import_id, worker_id, worker_url)
    store.record_inspection(import_id, worker, workflow_sha256=workflow_hash,
                            metadata_sha256=workflow_metadata_sha256(graph, object_info),
                            expected_binding_generation=binding_generation)
    return graph, object_info


def _analysis_payload(
    store: H3ProfileStore,
    import_id: str,
    analysis: Any,
) -> dict[str, Any]:
    payload = analysis.model_dump(mode="json")
    accepted_mapping = store.load_import_mapping(import_id)
    if accepted_mapping is not None:
        payload["mapping"] = accepted_mapping.model_dump(mode="json")
    payload["import_id"] = import_id
    payload["selected_output_node_id"] = store.load_import_output(import_id)
    payload["workflow_sha256"] = store.import_workflow_sha256(import_id)
    payload["lifecycle"] = store.import_lifecycle(import_id)
    return payload


@router.get("/imports/{import_id:path}/analysis", response_model=None)
async def analyze_h3_import(import_id: str, worker_id: str, worker_url: str) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        graph, object_info = await _inspect_import_metadata(store, import_id, worker_id, worker_url)
        analysis = inspect_h3_workflow(
            graph,
            object_info=object_info,
            output_node_id=store.load_import_output(import_id),
        )
        return _analysis_payload(store, import_id, analysis)
    except (TypeError, ValueError) as exc:
        return _error(400, "invalid_workflow", str(exc), {"import_id": import_id})
    except ProfileStorageError as exc:
        return _store_error(exc)


@router.put("/imports/{import_id:path}/output", response_model=None)
async def select_h3_import_output(
    import_id: str,
    body: SelectOutputRequest,
    worker_id: str,
    worker_url: str,
) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        graph, object_info = await _inspect_import_metadata(store, import_id, worker_id, worker_url)
        unselected = inspect_h3_workflow(graph, object_info=object_info)
        if body.node_id not in {
            candidate.node_id for candidate in unselected.output_candidates
        }:
            return _error(
                422,
                "output_selection_error",
                f"Node {body.node_id} is not an eligible terminal video output",
                {"import_id": import_id, "node_id": body.node_id},
            )
        store.save_import_output(import_id, body.node_id)
        selected = inspect_h3_workflow(
            graph,
            object_info=object_info,
            output_node_id=body.node_id,
        )
        return _analysis_payload(store, import_id, selected)
    except (TypeError, ValueError) as exc:
        return _error(400, "invalid_workflow", str(exc), {"import_id": import_id})
    except ProfileStorageError as exc:
        return _store_error(exc)


@router.put("/imports/{import_id:path}/mapping", response_model=None)
def save_h3_import_mapping(
    import_id: str,
    body: H3BoundaryMapping,
    worker_id: str,
    worker_url: str,
) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        _bound_worker(store, import_id, worker_id, worker_url)
        selected_output = store.load_import_output(import_id)
        if selected_output is None or body.output.node_id != selected_output:
            return _error(
                422,
                "input_selection_error",
                "The input mapping must use the confirmed final video output",
                {"import_id": import_id, "node_id": body.output.node_id},
            )
        report = validate_h3_contract(store.load_import_workflow(import_id), body)
        if not report.valid:
            return _error(
                422,
                "input_selection_error",
                "The confirmed H3 input boundary is invalid",
                {
                    "import_id": import_id,
                    "issues": [
                        issue.model_dump(mode="json") for issue in report.issues
                    ],
                },
            )
        store.save_import_mapping(import_id, body)
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {"import_id": import_id, "mapping": body.model_dump(mode="json")}


@router.post("/imports/{import_id:path}/validate", response_model=None)
async def validate_h3_import(import_id: str, worker_id: str, worker_url: str) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        worker, client = _bound_worker(store, import_id, worker_id, worker_url)
        binding_generation = store.import_binding_generation(import_id)
        graph, workflow_sha256 = store.load_import_workflow_snapshot(import_id)
        mapping = store.load_import_mapping(import_id)
        if mapping is None:
            return _error(
                422,
                "mapping_required",
                "A compatible workflow mapping is required before validation",
                {
                    "import_id": import_id,
                    "compatibility": "needs_confirmation",
                    "issues": [],
                },
            )
        report = validate_h3_contract(graph, mapping)
        if not report.valid:
            return _error(
                422,
                "contract_validation_failed",
                "The workflow does not satisfy the H3 Ref2AV contract",
                {
                    "import_id": import_id,
                    "issues": [
                        issue.model_dump(mode="json") for issue in report.issues
                    ],
                    "fixed_dependencies": [
                        item.model_dump(mode="json")
                        for item in report.fixed_dependencies
                    ],
                },
            )
        mapping_sha256 = store.mapping_sha256(mapping)
        filled = fill_profile_graph(
            ResolvedH3Profile(
                profile_id="validation-import",
                workflow=graph,
                mapping=mapping,
                workflow_sha256=workflow_sha256,
                source="custom",
            ),
            {
                "prompt": "Neutral H3 workflow validation",
                "images": ["contract-picture.png"],
                "audios": [],
                "frames": 56,
                "width": 864,
                "height": 480,
                "seed": 42,
                "output_prefix": "director-studio/h3/contract-validation",
            },
        )
    except (TypeError, ValueError) as exc:
        return _error(
            422, "contract_validation_failed", str(exc), {"import_id": import_id}
        )
    except ProfileStorageError as exc:
        return _store_error(exc)

    try:
        metadata = await client.get_object_info()
        _bound_worker(store, import_id, worker_id, worker_url)
        metadata_sha256 = workflow_metadata_sha256(graph, metadata)
        store.record_inspection(
            import_id,
            worker,
            workflow_sha256=workflow_sha256,
            metadata_sha256=metadata_sha256,
            expected_binding_generation=binding_generation,
        )
        comfy_payload = await client.validate_workflow(filled, object_info=metadata)
        comfy_payload["metadata_sha256"] = metadata_sha256
    except ComfyError as exc:
        return _error(
            422,
            "dependency_validation_failed",
            str(exc),
            {"import_id": import_id},
        )
    except ProfileStorageError as exc:
        return _store_error(exc)

    try:
        _bound_worker(store, import_id, worker_id, worker_url)
        record = store.record_validation_success(
            import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
            report=report.model_dump(mode="json"),
            comfy_payload=comfy_payload,
            worker=worker,
            expected_binding_generation=binding_generation,
        )
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {
        **report.model_dump(mode="json"),
        "import_id": import_id,
        "workflow_sha256": record["workflow_sha256"],
        "validated_at": record["validated_at"],
        "comfy": comfy_payload,
        "lifecycle": store.import_lifecycle(import_id),
    }


@router.post("/imports/{import_id:path}/test", status_code=202, response_model=None)
async def test_h3_import(
    import_id: str,
    body: TestProfileRequest,
) -> dict[str, Any] | JSONResponse:
    """Start an isolated remote test against a validated, unactivated import."""
    store = H3ProfileStore()
    try:
        _bound_worker(store, import_id, body.worker_id, body.worker_url)
        workflow_sha256, mapping_sha256 = store.testable_import_identity(import_id)
        mapping = store.load_import_mapping(import_id)
    except ProfileStorageError as exc:
        return _store_error(exc)

    picture = _resolve_picture_asset(body.picture_asset_id)
    if picture is None:
        return _error(
            404,
            "picture_asset_not_found",
            "A readable Picture asset is required for the H3 profile test",
            {"picture_asset_id": body.picture_asset_id},
        )

    inputs = {"picture_1": picture}
    audio_keys: list[str] = []
    audio_binding = ""
    if body.audio_asset_id is not None:
        if mapping is None or mapping.inputs.audio_input_pattern is None:
            return _error(
                422,
                "audio_not_supported",
                "This H3 workflow profile does not support reference Audio",
                {"import_id": import_id},
            )
        audio = _resolve_voice_asset(body.audio_asset_id)
        if audio is None:
            return _error(
                404,
                "audio_asset_not_found",
                "A readable H3-ready Voice asset is required",
                {"audio_asset_id": body.audio_asset_id},
            )
        inputs["audio_1"] = audio
        audio_keys.append("audio_1")
        audio_binding = " <Audio 1> defines the optional voice reference."

    job = create_job(
        pipeline_id="h3_ref2va",
        asset_kind="productions",
        name="H3 workflow profile test",
        notes="Isolated setup test; output is not attached to a shot or Asset Library.",
        params={
            "h3_provider": "local",
            "h3_profile_test": True,
            "worker_id": body.worker_id,
            "worker_url": validate_base_url(body.worker_url),
            "h3_profile_import_id": import_id,
            "h3_profile_test_workflow_sha256": workflow_sha256,
            "h3_profile_test_mapping_sha256": mapping_sha256,
            "h3_profile_test_boundary_sha256": store.boundary_sha256(mapping),
            "prompt": _TEST_PROMPT.format(audio_binding=audio_binding),
            "dialogue": [],
            "frames": 56,
            "width": 864,
            "height": 480,
            "image_keys": ["picture_1"],
            "audio_keys": audio_keys,
        },
        seed=42,
        fixed_seed=True,
    )
    try:
        store.record_test_submission(import_id, job)
        await start_pipeline_job(job, images=inputs)
    except ProfileStorageError as exc:
        _fail_unstarted_test(job.id, exc)
        return _store_error(exc)
    except (TypeError, ValueError) as exc:
        _fail_unstarted_test(job.id, exc)
        return _error(
            422,
            "test_job_invalid",
            str(exc),
            {"import_id": import_id},
        )
    return {
        "import_id": import_id,
        "job_id": job.id,
        "job_url": f"/api/h3-ref2va/jobs/{job.id}",
        "workflow_sha256": workflow_sha256,
        "mapping_sha256": mapping_sha256,
        "status": "queued",
    }


def _fail_unstarted_test(job_id: str, exc: Exception) -> None:
    job = job_store.load_job(job_id)
    if job is not None and job.status == JobStatus.queued:
        job.status = JobStatus.failed
        job.error = f"Job preparation failed: {exc}"
        job_store.save_job(job)


@router.put("/imports/{import_id:path}/test-output", response_model=None)
def select_h3_test_output(
    import_id: str,
    body: SelectTestOutputRequest,
    worker_id: str,
    worker_url: str,
) -> dict[str, Any] | JSONResponse:
    """Choose one already-generated setup-test video without rerunning Comfy."""
    store = H3ProfileStore()
    try:
        _bound_worker(store, import_id, worker_id, worker_url)
        record = store.select_test_output(import_id, body.artifact_index)
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {
        "import_id": import_id,
        "artifact_index": record["artifact_index"],
        "job_id": record["job_id"],
        "status": record["status"],
        "lifecycle": store.import_lifecycle(import_id),
    }


@router.post("/imports/{import_id:path}/activate", response_model=None)
async def activate_h3_import(import_id: str, worker_id: str, worker_url: str) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        _worker, client = _bound_worker(store, import_id, worker_id, worker_url)
        binding_generation = store.import_binding_generation(import_id)
        await client.health()
        _bound_worker(store, import_id, worker_id, worker_url)
        if store.import_binding_generation(import_id) != binding_generation:
            raise ProfileChangedError(
                "Render worker binding changed during activation; select the worker again"
            )
        profile = store.activate_import(import_id)
    except ComfyError as exc:
        return _error(
            503, "worker_unavailable", str(exc), {"worker_id": worker_id}
        )
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {
        "import_id": import_id,
        "profile_id": profile.id,
        "active": _active_payload(store),
    }


@router.post("/select", response_model=None)
def select_h3_profile(body: SelectProfileRequest) -> dict[str, Any] | JSONResponse:
    store = H3ProfileStore()
    try:
        store.select_profile(body.profile_id)
    except ProfileStorageError as exc:
        return _store_error(exc)
    return {"active": _active_payload(store)}


__all__ = ["router"]
