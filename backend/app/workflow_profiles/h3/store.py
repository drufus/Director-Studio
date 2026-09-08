"""Safe persistent storage and resolution for H3 workflow profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.config import settings

from .errors import (
    ProfileChangedError,
    ProfileStateError,
    ProfileStorageError,
)
from .models import (
    H3BoundaryMapping,
    H3InputMapping,
    H3OutputSelection,
    H3WorkflowProfile,
    H3WorkerBinding,
    ResolvedH3Profile,
)

if TYPE_CHECKING:
    from app.core.schemas import JobRecord

_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_IMPORT_ID_RE = re.compile(r"imp-[a-f0-9]{32}\Z")
_BUILTIN_PROFILE_ID = "builtin-official-h3"
_WORKFLOW_FILE = "workflow.api.json"
_PROFILE_FILE = "profile.json"
_ACTIVE_FILE = "active.json"
_MAPPING_FILE = "mapping.json"
_OUTPUT_FILE = "output.json"
_VALIDATION_FILE = "validation.json"
_TEST_FILE = "test.json"
_IMPORT_FILE = "import.json"
_WORKER_FILE = "worker.json"
_INSPECTION_FILE = "inspection.json"
_PROOFS_FILE = "worker_proofs.json"
_JOB_SNAPSHOT_DIR = "workflow_profile"
_H3_REF2AV_NODE = "MiniMaxH3ReferenceToVideo"
_H3_I2V_NODE = "MiniMaxH3ImageToVideo"

_OFFICIAL_MAPPING = H3BoundaryMapping(
    inputs=H3InputMapping(
        h3_node_id="136",
        prompt_input="prompt",
        width_input="width",
        height_input="height",
        frames_input="length",
        picture_input_pattern="ref_images.ref_image_{index}",
        audio_input_pattern="ref_audios.ref_audio_{index}",
        seed_node_id="129",
        seed_input="noise_seed",
    ),
    output=H3OutputSelection(node_id="92"),
)


class H3ProfileStore:
    """Own H3 profile files below the external persistent-data root only."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.workflow_profiles_dir) / "h3"

    @property
    def imports_dir(self) -> Path:
        return self.root / "imports"

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def active_path(self) -> Path:
        return self.root / _ACTIVE_FILE

    def workflow_path(self, profile_id: str) -> Path:
        return self._safe_path(self._profile_dir(profile_id) / _WORKFLOW_FILE)

    def profile_path(self, profile_id: str) -> Path:
        return self._safe_path(self._profile_dir(profile_id) / _PROFILE_FILE)

    def import_workflow_path(self, import_id: str) -> Path:
        return self._safe_path(self._import_dir(import_id) / _WORKFLOW_FILE)

    def import_workflow_sha256(self, import_id: str) -> str:
        """Return the digest of the currently stored import bytes."""
        _workflow, workflow_sha256 = self.load_import_workflow_snapshot(import_id)
        return workflow_sha256

    def load_import_workflow(self, import_id: str) -> dict[str, Any]:
        """Load an import by opaque ID, never by a caller-provided path."""
        workflow, _workflow_sha256 = self.load_import_workflow_snapshot(import_id)
        return workflow

    def load_import_workflow_snapshot(
        self, import_id: str
    ) -> tuple[dict[str, Any], str]:
        """Load graph and digest from the same immutable byte snapshot."""
        return self._read_workflow(self.import_workflow_path(import_id))

    def bind_import_worker(
        self, import_id: str, binding: H3WorkerBinding | None
    ) -> H3WorkerBinding | None:
        """Explicitly choose or clear a worker, invalidating all worker evidence."""
        directory = self._require_existing_import(import_id)
        current_record = self._optional_record(directory / _WORKER_FILE)
        current = self._binding_from_record(current_record, required=False)
        if current == binding and isinstance(
            (current_record or {}).get("binding_generation"), str
        ):
            return binding
        # Persist the new identity first. Even an interrupted invalidation makes
        # old evidence fail its worker comparison.
        self._atomic_write_json(
            directory / _WORKER_FILE,
            {
                "worker": binding.model_dump(mode="json") if binding else None,
                "binding_generation": secrets.token_hex(16),
                "invalidation_reason": "Render worker changed; inspect, validate, and test again",
            },
        )
        self._invalidate_records(
            directory, (_INSPECTION_FILE, _VALIDATION_FILE, _TEST_FILE)
        )
        return binding

    def require_import_worker(
        self, import_id: str, binding: H3WorkerBinding | None = None
    ) -> H3WorkerBinding:
        record = self._optional_record(
            self._require_existing_import(import_id) / _WORKER_FILE
        )
        current = self._binding_from_record(record)
        if binding is not None and current != binding:
            raise ProfileStateError(
                "worker_mismatch",
                "Import render worker changed; repeat the operation on its selected worker",
                details={
                    "import_id": import_id,
                    "expected_worker": current.model_dump(mode="json"),
                    "requested_worker": binding.model_dump(mode="json"),
                },
            )
        return current

    def import_binding_generation(self, import_id: str) -> str:
        record = self._optional_record(
            self._require_existing_import(import_id) / _WORKER_FILE
        )
        generation = (record or {}).get("binding_generation")
        if not isinstance(generation, str) or not re.fullmatch(
            r"[0-9a-f]{32}", generation
        ):
            raise ProfileStateError(
                "worker_binding_changed",
                "Select the render worker again; its binding generation is unavailable",
            )
        return generation

    @staticmethod
    def _binding_from_record(
        record: dict[str, Any] | None, *, required: bool = True
    ) -> H3WorkerBinding | None:
        value = (record or {}).get("worker")
        if value is None and not required:
            return None
        if value is None:
            raise ProfileStateError(
                "worker_required",
                "Select a render worker; worker-bound H3 evidence is required",
            )
        try:
            return H3WorkerBinding.model_validate(value)
        except (ValidationError, ValueError) as exc:
            raise ProfileStorageError(
                "Stored H3 render worker binding is invalid"
            ) from exc

    def _invalidate_records(self, directory: Path, names: tuple[str, ...]) -> None:
        for name in names:
            try:
                self._safe_path(directory / name, write=True).unlink(missing_ok=True)
            except OSError as exc:
                raise ProfileStorageError(
                    "Could not invalidate stale worker evidence"
                ) from exc

    def record_inspection(
        self,
        import_id: str,
        binding: H3WorkerBinding,
        *,
        workflow_sha256: str,
        metadata_sha256: str,
        expected_binding_generation: str | None = None,
    ) -> dict[str, Any]:
        self.require_import_worker(import_id, binding)
        if (
            expected_binding_generation is not None
            and expected_binding_generation != self.import_binding_generation(import_id)
        ):
            raise ProfileChangedError(
                "Render worker binding changed during inspection; inspect again"
            )
        if self.import_workflow_sha256(import_id) != workflow_sha256:
            raise ProfileChangedError("Imported workflow changed during inspection")
        if not isinstance(metadata_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", metadata_sha256
        ):
            raise ProfileStorageError("Worker metadata digest is invalid")
        directory = self._require_existing_import(import_id)
        generation = self.import_binding_generation(import_id)
        record = {
            "worker": binding.model_dump(mode="json"),
            "binding_generation": generation,
            "workflow_sha256": workflow_sha256,
            "metadata_sha256": metadata_sha256,
            "inspected_at": datetime.now(UTC).isoformat(),
        }
        previous = self._optional_record(directory / _INSPECTION_FILE)
        changed = previous is not None and any(
            previous.get(key) != record[key]
            for key in (
                "worker",
                "binding_generation",
                "workflow_sha256",
                "metadata_sha256",
            )
        )
        self._atomic_write_json(directory / _INSPECTION_FILE, record)
        if changed:
            self._invalidate_records(directory, (_VALIDATION_FILE, _TEST_FILE))
        self._atomic_write_json(
            directory / _WORKER_FILE,
            {
                "worker": binding.model_dump(mode="json"),
                "binding_generation": generation,
                "invalidation_reason": "Worker metadata changed; validate and test again"
                if changed
                else None,
            },
        )
        return record

    def _require_current_inspection(
        self, import_id: str, worker: H3WorkerBinding, workflow_sha256: str
    ) -> dict[str, Any]:
        self.require_import_worker(import_id, worker)
        inspection = self._optional_record(
            self._require_existing_import(import_id) / _INSPECTION_FILE
        )
        if inspection is None:
            raise ProfileStateError(
                "inspection_required",
                "Inspect this workflow on its selected render worker first",
            )
        metadata = inspection.get("metadata_sha256")
        if not isinstance(metadata, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata):
            raise ProfileStorageError(
                "Stored worker inspection metadata digest is invalid"
            )
        if (
            self._binding_from_record(inspection) != worker
            or inspection.get("binding_generation")
            != self.import_binding_generation(import_id)
            or inspection.get("workflow_sha256") != workflow_sha256
        ):
            raise ProfileChangedError(
                "Workflow inspection no longer matches the workflow and selected worker"
            )
        return inspection

    def save_import_output(self, import_id: str, node_id: str) -> None:
        """Persist an output choice and invalidate dependent mapping evidence."""
        if not isinstance(node_id, str) or not node_id:
            raise ProfileStorageError("A workflow output node ID is required")
        directory = self._require_existing_import(import_id)
        current = self.load_import_output(import_id)
        self._atomic_write_json(directory / _OUTPUT_FILE, {"node_id": node_id})
        if current == node_id:
            return
        for name in (_MAPPING_FILE, _VALIDATION_FILE, _TEST_FILE):
            try:
                self._safe_path(directory / name, write=True).unlink(missing_ok=True)
            except OSError as exc:
                raise ProfileStorageError(
                    "Could not invalidate stale workflow boundary evidence"
                ) from exc

    def load_import_output(self, import_id: str) -> str | None:
        """Load the confirmed output node for an import, if selected."""
        path = self._safe_path(self._require_existing_import(import_id) / _OUTPUT_FILE)
        if not path.exists():
            return None
        record = self._read_json(path)
        node_id = record.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ProfileStorageError("Stored workflow output selection is invalid")
        return node_id

    def save_import_mapping(
        self,
        import_id: str,
        mapping: H3BoundaryMapping,
    ) -> None:
        """Persist an accepted mapping and invalidate mapping-specific evidence."""
        directory = self._require_existing_import(import_id)
        stale_paths = [
            self._safe_path(directory / name, write=True)
            for name in (_VALIDATION_FILE, _TEST_FILE)
        ]
        self._atomic_write_json(
            directory / _MAPPING_FILE,
            mapping.model_dump(mode="json"),
        )
        for stale_path in stale_paths:
            try:
                self._safe_path(stale_path, write=True).unlink(missing_ok=True)
            except OSError as exc:
                raise ProfileStorageError(
                    "Could not invalidate stale workflow profile evidence"
                ) from exc

    def save_import_artifact_index(
        self, import_id: str, artifact_index: int
    ) -> H3BoundaryMapping:
        """Select an observed output without invalidating graph validation."""
        if (
            not isinstance(artifact_index, int)
            or isinstance(artifact_index, bool)
            or artifact_index < 0
        ):
            raise ProfileStorageError(
                "A non-negative output artifact index is required"
            )
        directory = self._require_existing_import(import_id)
        mapping = self.load_import_mapping(import_id)
        if mapping is None:
            raise ProfileStateError(
                "mapping_required", "A workflow mapping is required"
            )
        selected = mapping.model_copy(
            update={
                "output": mapping.output.model_copy(
                    update={"artifact_index": artifact_index}
                )
            }
        )
        if self.boundary_sha256(selected) != self.boundary_sha256(mapping):
            raise ProfileChangedError("Output selection changed the workflow boundary")
        self._atomic_write_json(
            directory / _MAPPING_FILE, selected.model_dump(mode="json")
        )
        validation = self._optional_record(directory / _VALIDATION_FILE)
        if validation is not None:
            validation["mapping_sha256"] = self.mapping_sha256(selected)
            self._atomic_write_json(directory / _VALIDATION_FILE, validation)
        return selected

    def load_import_mapping(self, import_id: str) -> H3BoundaryMapping | None:
        """Load the selected mapping, or return none before one is accepted."""
        path = self._safe_path(self._require_existing_import(import_id) / _MAPPING_FILE)
        if not path.exists():
            return None
        try:
            return H3BoundaryMapping.model_validate(self._read_json(path))
        except ValidationError as exc:
            raise ProfileStorageError("Stored import mapping is invalid") from exc

    def record_validation_success(
        self,
        import_id: str,
        *,
        workflow_sha256: str,
        mapping_sha256: str,
        report: dict[str, Any],
        comfy_payload: dict[str, Any],
        worker: H3WorkerBinding,
        expected_binding_generation: str | None = None,
    ) -> dict[str, Any]:
        """Persist successful contract and live-Comfy validation evidence."""
        if (
            expected_binding_generation is not None
            and expected_binding_generation != self.import_binding_generation(import_id)
        ):
            raise ProfileChangedError(
                "Render worker binding changed during validation; validate again"
            )
        current_workflow_sha256, current_mapping_sha256 = self.import_identity(
            import_id
        )
        if (
            current_workflow_sha256 != workflow_sha256
            or current_mapping_sha256 != mapping_sha256
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed during validation"
            )
        inspection = self._require_current_inspection(
            import_id, worker, workflow_sha256
        )
        if comfy_payload.get("metadata_sha256") != inspection.get("metadata_sha256"):
            raise ProfileStateError(
                "inspection_changed",
                "Worker metadata changed since inspection; inspect again before validation",
            )
        record = {
            "worker": worker.model_dump(mode="json"),
            "inspection": inspection,
            "valid": True,
            "contract_version": 2,
            "workflow_sha256": workflow_sha256,
            "mapping_sha256": mapping_sha256,
            "validated_at": datetime.now(UTC).isoformat(),
            "display_name": self._display_name(
                self._optional_record(
                    self._require_existing_import(import_id) / _IMPORT_FILE
                )
            ),
            "report": report,
            "comfy": comfy_payload,
        }
        self._atomic_write_json(
            self._require_existing_import(import_id) / _VALIDATION_FILE,
            record,
        )
        return record

    def testable_import_identity(self, import_id: str) -> tuple[str, str]:
        """Return one validated import identity suitable for test submission."""
        workflow_sha256, mapping_sha256 = self.import_identity(import_id)
        self._require_current_validation(
            self._require_existing_import(import_id),
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
        )
        return workflow_sha256, mapping_sha256

    def record_test_submission(self, import_id: str, job: JobRecord) -> dict[str, Any]:
        """Record a test intent before its background task or snapshot can start."""
        worker = self.require_import_worker(import_id)
        workflow_hash, mapping_hash = self.testable_import_identity(import_id)
        self._require_test_intent(job, import_id, worker, workflow_hash, mapping_hash)
        inspection = self._require_current_inspection(import_id, worker, workflow_hash)
        self._capture_test_inspection(job, inspection)
        from app.core.jobs.store import save_job

        save_job(job)
        record = {
            "status": "submitted",
            "binding_generation": inspection["binding_generation"],
            "metadata_sha256": inspection["metadata_sha256"],
            "contract_version": 2,
            "worker": worker.model_dump(mode="json"),
            "workflow_sha256": workflow_hash,
            "mapping_sha256": mapping_hash,
            "boundary_sha256": self.boundary_sha256(
                self.load_import_mapping(import_id)
            ),
            "job_id": job.id,
            "submitted_at": datetime.now(UTC).isoformat(),
        }
        self._atomic_write_json(
            self._require_existing_import(import_id) / _TEST_FILE, record
        )
        return record

    @staticmethod
    def _capture_test_inspection(job: JobRecord, inspection: dict[str, Any]) -> None:
        params = dict(job.params or {})
        for field in ("binding_generation", "metadata_sha256"):
            key = f"h3_profile_test_{field}"
            if key in params and params[key] != inspection[field]:
                raise ProfileChangedError(
                    "H3 test belongs to an earlier worker binding or metadata inspection; submit a new test"
                )
            params[key] = inspection[field]
        job.params = params

    def _require_test_intent(
        self,
        job: JobRecord,
        import_id: str,
        worker: H3WorkerBinding,
        workflow_hash: str,
        mapping_hash: str,
    ) -> None:
        self._require_job_worker(job, worker, durable=False)
        params = job.params or {}
        inspection = self._require_current_inspection(import_id, worker, workflow_hash)
        for field in ("binding_generation", "metadata_sha256"):
            key = f"h3_profile_test_{field}"
            if key in params and params[key] != inspection[field]:
                raise ProfileChangedError(
                    "Pending H3 test belongs to an earlier worker binding or metadata inspection"
                )
        if (
            job.pipeline_id != "h3_ref2va"
            or params.get("h3_profile_test") is not True
            or params.get("h3_profile_import_id") != import_id
            or params.get("h3_profile_test_workflow_sha256") != workflow_hash
            or params.get("h3_profile_test_mapping_sha256") != mapping_hash
            or params.get("h3_profile_test_boundary_sha256")
            != self.boundary_sha256(self.load_import_mapping(import_id))
            or job.project_id is not None
            or job.library_asset_id is not None
            or "shot_id" in params
            or "project_id" in params
        ):
            raise ProfileChangedError(
                "H3 test intent does not match its import identity and worker"
            )

    def record_test_success(
        self,
        import_id: str,
        *,
        workflow_sha256: str,
        mapping_sha256: str,
        job_id: str,
        boundary_sha256: str | None = None,
        artifact_index: int | None = None,
    ) -> dict[str, Any]:
        """Persist evidence only for an already durable successful test job."""
        directory = self._require_existing_import(import_id)
        current_workflow_sha256, current_mapping_sha256 = self.import_identity(
            import_id
        )
        if (
            current_workflow_sha256 != workflow_sha256
            or current_mapping_sha256 != mapping_sha256
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed during test execution"
            )
        boundary_sha256 = boundary_sha256 or self.boundary_sha256(
            self.load_import_mapping(import_id)
        )
        job = self._require_successful_test_job(
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
            boundary_sha256=boundary_sha256,
            job_id=job_id,
        )
        worker = self.require_import_worker(import_id)
        self._require_current_validation(
            directory,
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
        )
        inspection = self._require_current_inspection(
            import_id, worker, workflow_sha256
        )
        candidates = self._test_video_candidates(job)
        if artifact_index is None and len(candidates) == 1:
            artifact_index = 0
        if artifact_index is None:
            record = {
                "worker": worker.model_dump(mode="json"),
                "metadata_sha256": inspection["metadata_sha256"],
                "binding_generation": inspection["binding_generation"],
                "status": "awaiting_selection",
                "contract_version": 2,
                "workflow_sha256": workflow_sha256,
                "mapping_sha256": mapping_sha256,
                "boundary_sha256": boundary_sha256,
                "job_id": job_id,
                "candidates": candidates,
            }
            self._atomic_write_json(directory / _TEST_FILE, record)
            return record
        if artifact_index >= len(candidates):
            raise ProfileStateError(
                "test_output_required",
                "The selected test video output does not exist",
                details={
                    "artifact_index": artifact_index,
                    "candidate_count": len(candidates),
                },
            )
        selected_mapping = self.save_import_artifact_index(import_id, artifact_index)
        selected_mapping_sha256 = self.mapping_sha256(selected_mapping)
        if self.import_workflow_sha256(import_id) != workflow_sha256:
            raise ProfileChangedError(
                "Imported workflow or mapping changed during test execution"
            )
        record = {
            "worker": worker.model_dump(mode="json"),
            "metadata_sha256": inspection["metadata_sha256"],
            "binding_generation": inspection["binding_generation"],
            "status": "succeeded",
            "contract_version": 2,
            "workflow_sha256": workflow_sha256,
            "mapping_sha256": selected_mapping_sha256,
            "boundary_sha256": boundary_sha256,
            "job_id": job_id,
            "artifact_index": artifact_index,
            "candidates": candidates,
            "tested_at": datetime.now(UTC).isoformat(),
        }
        self._atomic_write_json(directory / _TEST_FILE, record)
        return record

    def select_test_output(self, import_id: str, artifact_index: int) -> dict[str, Any]:
        """Finalize an awaiting setup test using one observed candidate."""
        directory = self._require_existing_import(import_id)
        pending = self._optional_record(directory / _TEST_FILE)
        if pending is None or pending.get("status") != "awaiting_selection":
            raise ProfileStateError(
                "test_output_required",
                "No completed H3 workflow test is awaiting output selection",
                details={"import_id": import_id},
            )
        if self._binding_from_record(pending) != self.require_import_worker(import_id):
            raise ProfileChangedError(
                "Test output evidence belongs to a different render worker"
            )
        return self.record_test_success(
            import_id,
            workflow_sha256=pending.get("workflow_sha256"),
            mapping_sha256=pending.get("mapping_sha256"),
            boundary_sha256=pending.get("boundary_sha256"),
            job_id=pending.get("job_id"),
            artifact_index=artifact_index,
        )

    def activate_import(self, import_id: str) -> H3WorkflowProfile:
        """Install and select an import only with same-identity validation/test proof."""
        directory = self._require_existing_import(import_id)
        workflow, workflow_sha256 = self._read_workflow(directory / _WORKFLOW_FILE)
        mapping = self.load_import_mapping(import_id)
        if mapping is None:
            raise ProfileStateError(
                "mapping_required",
                "A workflow mapping is required before activation",
                details={"import_id": import_id},
            )
        mapping_sha256 = self.mapping_sha256(mapping)
        validation = self._require_current_validation(
            directory,
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
        )

        test_record = self._optional_record(directory / _TEST_FILE)
        if test_record is None or test_record.get("status") != "succeeded":
            raise ProfileStateError(
                "test_required",
                "A successful test for this workflow is required before activation",
                details={"import_id": import_id},
            )
        if self._binding_from_record(test_record) != self.require_import_worker(
            import_id
        ):
            raise ProfileChangedError(
                "Successful test belongs to a different render worker"
            )
        if test_record.get("contract_version") != 2:
            raise ProfileStateError(
                "unsupported_contract",
                "The tested workflow contract version is unsupported",
                details={"import_id": import_id},
            )
        if (
            test_record.get("workflow_sha256") != workflow_sha256
            or test_record.get("mapping_sha256") != mapping_sha256
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed after its successful test"
            )
        if not isinstance(test_record.get("job_id"), str) or not test_record["job_id"]:
            raise ProfileStateError(
                "test_required",
                "A successful test job is required before activation",
                details={"import_id": import_id},
            )
        self._require_successful_test_job(
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
            boundary_sha256=test_record.get("boundary_sha256"),
            job_id=test_record["job_id"],
        )
        if self.import_identity(import_id) != (
            workflow_sha256,
            mapping_sha256,
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed during activation"
            )

        from .validator import validate_h3_contract

        contract = validate_h3_contract(workflow, mapping)
        if not contract.valid:
            raise ProfileStateError(
                "contract_validation_failed",
                "The workflow no longer satisfies the H3 contract",
                details={
                    "issues": [
                        issue.model_dump(mode="json") for issue in contract.issues
                    ]
                },
            )

        profile_id = f"custom-{workflow_sha256[:32]}-{mapping_sha256[:16]}"
        profile = H3WorkflowProfile(
            id=profile_id,
            workflow_sha256=workflow_sha256,
            mapping=mapping,
            status="active",
        )
        self.install_profile(
            profile,
            workflow,
            validation_record=validation,
            test_record=test_record,
        )
        if self.import_identity(import_id) != (
            workflow_sha256,
            mapping_sha256,
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed during activation"
            )
        self.select_profile(profile_id)
        return H3WorkflowProfile.model_validate(
            self._read_json(self.profile_path(profile_id))
        )

    def list_installed_profiles(self) -> list[H3WorkflowProfile]:
        """Return valid installed custom profile metadata in stable ID order."""
        try:
            directories = list(self._safe_path(self.profiles_dir).iterdir())
        except (ProfileStorageError, OSError):
            return []
        profiles: list[H3WorkflowProfile] = []
        for directory in sorted(directories, key=lambda path: path.name):
            try:
                if not self._safe_path(directory).is_dir():
                    continue
                profile_id = self._require_profile_id(directory.name)
                profile = H3WorkflowProfile.model_validate(
                    self._read_json(self.profile_path(profile_id))
                )
            except (ProfileStorageError, ValidationError):
                continue
            profiles.append(profile)
        return profiles

    def create_import(
        self, workflow: dict[str, Any], *, display_name: str = "Custom H3 workflow"
    ) -> str:
        """Persist an API workflow under a generated opaque import identifier."""
        if not isinstance(workflow, dict):
            raise ProfileStorageError("Imported workflow must be a JSON object")
        self._mkdir(self.imports_dir)
        for _ in range(10):
            import_id = f"imp-{secrets.token_hex(16)}"
            import_dir = self._import_dir(import_id)
            try:
                self._safe_path(import_dir, write=True).mkdir()
            except FileExistsError:
                continue
            self._atomic_write_json(import_dir / _WORKFLOW_FILE, workflow)
            self._atomic_write_json(
                import_dir / _IMPORT_FILE,
                {
                    "display_name": self._display_name({"display_name": display_name}),
                },
            )
            return import_id
        raise ProfileStorageError("Could not allocate a unique workflow import ID")

    def _require_existing_import(self, import_id: str) -> Path:
        directory = self._import_dir(import_id)
        if not self._safe_path(directory).is_dir():
            raise ProfileStorageError("Workflow import was not found")
        return directory

    def import_identity(self, import_id: str) -> tuple[str, str]:
        """Return the current workflow and mapping hashes for evidence binding."""
        workflow_sha256 = self.import_workflow_sha256(import_id)
        mapping = self.load_import_mapping(import_id)
        if mapping is None:
            raise ProfileStateError(
                "mapping_required",
                "A workflow mapping is required",
                details={"import_id": import_id},
            )
        return workflow_sha256, self.mapping_sha256(mapping)

    def import_lifecycle(self, import_id: str) -> dict[str, Any]:
        """Expose bounded current evidence for reloadable setup, never file paths."""
        workflow_sha256 = self.import_workflow_sha256(import_id)
        mapping = self.load_import_mapping(import_id)
        mapping_sha256 = self.mapping_sha256(mapping) if mapping else None
        directory = self._require_existing_import(import_id)
        worker_record = self._optional_record(directory / _WORKER_FILE)
        worker = self._binding_from_record(worker_record, required=False)
        inspection = self._optional_record(directory / _INSPECTION_FILE)
        result: dict[str, Any] = {
            "worker": worker.model_dump(mode="json") if worker else None,
            "inspected_at": inspection.get("inspected_at") if inspection else None,
            "metadata_sha256": inspection.get("metadata_sha256")
            if inspection
            else None,
            "invalidation_reason": (worker_record or {}).get("invalidation_reason"),
            "status": "mapped" if mapping else "draft",
            "workflow_sha256": workflow_sha256,
            "mapping_sha256": mapping_sha256,
            "validated_at": None,
            "test_job_id": None,
        }
        if mapping_sha256 is None:
            return result
        directory = self._require_existing_import(import_id)
        try:
            validation = self._require_current_validation(
                directory,
                import_id=import_id,
                workflow_sha256=workflow_sha256,
                mapping_sha256=mapping_sha256,
            )
        except ProfileStorageError as exc:
            if self._optional_record(directory / _VALIDATION_FILE) is not None:
                result["invalidation_reason"] = str(exc)
            return result
        result.update(status="validated", validated_at=validation.get("validated_at"))
        try:
            test = self._optional_record(directory / _TEST_FILE)
            if test and test.get("status") in {"submitted", "awaiting_selection"}:
                from app.core.jobs.store import job_dir
                from app.core.schemas import JobRecord

                job_id = test.get("job_id")
                if not isinstance(job_id, str) or not re.fullmatch(
                    r"job_[a-f0-9]{12}", job_id
                ):
                    raise ProfileStorageError(
                        "Pending H3 workflow test job ID is invalid"
                    )
                if (
                    test.get("contract_version") != 2
                    or self._binding_from_record(test) != worker
                    or test.get("workflow_sha256") != workflow_sha256
                    or test.get("mapping_sha256") != mapping_sha256
                ):
                    raise ProfileChangedError(
                        "Pending H3 test no longer matches its workflow and selected worker"
                    )
                try:
                    pending_job = JobRecord.model_validate(
                        self._read_json(job_dir(job_id) / "job.json")
                    )
                except ValidationError as exc:
                    raise ProfileStorageError(
                        "Pending H3 workflow test job is invalid"
                    ) from exc
                self._require_test_intent(
                    pending_job, import_id, worker, workflow_sha256, mapping_sha256
                )
                if test.get("status") == "awaiting_selection":
                    self._require_successful_test_job(
                        import_id=import_id,
                        workflow_sha256=workflow_sha256,
                        mapping_sha256=mapping_sha256,
                        boundary_sha256=test.get("boundary_sha256"),
                        job_id=job_id,
                    )
                return {
                    **result,
                    "test_job_id": job_id,
                    "test_status": pending_job.status.value,
                }
            if not test or (
                test.get("status") != "succeeded"
                or test.get("contract_version") != 2
                or test.get("workflow_sha256") != workflow_sha256
                or test.get("mapping_sha256") != mapping_sha256
            ):
                return result
            if self._binding_from_record(test) != worker:
                raise ProfileChangedError(
                    "Test evidence belongs to a different render worker"
                )
            self._require_successful_test_job(
                import_id=import_id,
                workflow_sha256=workflow_sha256,
                mapping_sha256=mapping_sha256,
                boundary_sha256=test.get("boundary_sha256"),
                job_id=test.get("job_id"),
            )
            if self.import_identity(import_id) != (workflow_sha256, mapping_sha256):
                return {**result, "status": "mapped", "validated_at": None}
        except ProfileStorageError as exc:
            result["invalidation_reason"] = str(exc)
            return result
        result.update(status="tested", test_job_id=test["job_id"])
        return result

    @classmethod
    def mapping_sha256(cls, mapping: H3BoundaryMapping) -> str:
        """Hash one exact mapping snapshot using the store's canonical JSON."""
        return cls._sha256(cls._json_bytes(mapping.model_dump(mode="json")))

    @classmethod
    def boundary_sha256(cls, mapping: H3BoundaryMapping) -> str:
        """Hash the submitted-graph boundary, excluding post-test artifact choice."""
        payload = mapping.model_dump(mode="json")
        payload["output"]["artifact_index"] = None
        return cls._sha256(cls._json_bytes(payload))

    @classmethod
    def _profile_sha256(cls, profile: H3WorkflowProfile) -> str:
        return cls._sha256(cls._json_bytes(profile.model_dump(mode="json")))

    def _optional_record(self, path: Path) -> dict[str, Any] | None:
        if not self._safe_path(path).exists():
            return None
        return self._read_json(path)

    def _require_current_validation(
        self,
        directory: Path,
        *,
        import_id: str,
        workflow_sha256: str,
        mapping_sha256: str,
    ) -> dict[str, Any]:
        validation = self._optional_record(directory / _VALIDATION_FILE)
        if validation is None or validation.get("valid") is not True:
            raise ProfileStateError(
                "validation_required",
                "Successful validation is required before testing or activation",
                details={"import_id": import_id},
            )
        if validation.get("contract_version") != 2:
            raise ProfileStateError(
                "unsupported_contract",
                "The validated workflow contract version is unsupported",
                details={"import_id": import_id},
            )
        if (
            validation.get("workflow_sha256") != workflow_sha256
            or validation.get("mapping_sha256") != mapping_sha256
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed after validation"
            )
        worker = self.require_import_worker(import_id)
        inspection = self._require_current_inspection(
            import_id, worker, workflow_sha256
        )
        validation_inspection = validation.get("inspection")
        if not isinstance(validation_inspection, dict):
            raise ProfileStorageError(
                "Stored validation has no worker inspection evidence"
            )
        if (
            self._binding_from_record(validation) != worker
            or self._binding_from_record(validation_inspection) != worker
            or validation_inspection.get("workflow_sha256") != workflow_sha256
            or validation_inspection.get("binding_generation")
            != inspection.get("binding_generation")
            or validation_inspection.get("metadata_sha256")
            != inspection.get("metadata_sha256")
        ):
            raise ProfileChangedError(
                "Validation no longer matches its selected worker and inspected metadata"
            )
        report = validation.get("report")
        comfy = validation.get("comfy")
        if (
            not isinstance(report, dict)
            or report.get("valid") is not True
            or not isinstance(comfy, dict)
            or comfy.get("valid") is not True
            or comfy.get("metadata_sha256") != inspection.get("metadata_sha256")
        ):
            raise ProfileStateError(
                "validation_required",
                "Successful contract and Comfy validation is required",
                details={"import_id": import_id},
            )
        return validation

    def _require_successful_test_job(
        self,
        *,
        import_id: str,
        workflow_sha256: str,
        mapping_sha256: str,
        boundary_sha256: str | None = None,
        job_id: str,
    ) -> JobRecord:
        """Verify activation evidence against the authoritative durable job."""
        from app.core.jobs.store import job_dir
        from app.core.schemas import JobRecord, JobStatus

        if not isinstance(job_id, str) or not re.fullmatch(r"job_[a-f0-9]{12}", job_id):
            raise ProfileStateError(
                "test_required",
                "The referenced H3 profile test job is invalid",
                details={"import_id": import_id},
            )
        job_record_path = job_dir(job_id) / "job.json"
        try:
            job = JobRecord.model_validate(self._read_json(job_record_path))
        except ValidationError as exc:
            raise ProfileStorageError("Stored H3 profile test job is invalid") from exc
        if job is None or job.status != JobStatus.succeeded:
            raise ProfileStateError(
                "test_required",
                "The referenced H3 profile test job did not succeed",
                details={"import_id": import_id, "job_id": job_id},
            )
        worker = self.require_import_worker(import_id)
        self._require_job_worker(job, worker, durable=True)
        params = job.params or {}
        if (
            job.id != job_id
            or job.pipeline_id != "h3_ref2va"
            or params.get("h3_profile_test") is not True
            or params.get("h3_profile_import_id") != import_id
            or params.get("h3_contract_version") != 2
            or job.project_id is not None
            or job.library_asset_id is not None
            or "shot_id" in params
            or "project_id" in params
        ):
            raise ProfileStateError(
                "test_required",
                "The referenced job is not an isolated H3 profile test job",
                details={"import_id": import_id, "job_id": job_id},
            )
        if (
            params.get("h3_profile_id") != import_id
            or params.get("h3_profile_sha256") != workflow_sha256
            or params.get("h3_profile_test_workflow_sha256") != workflow_sha256
            or (
                boundary_sha256 is None
                and params.get("h3_profile_test_mapping_sha256") != mapping_sha256
            )
            or (
                boundary_sha256 is not None
                and params.get("h3_profile_test_boundary_sha256") != boundary_sha256
            )
        ):
            raise ProfileChangedError(
                "H3 profile test job identity does not match its evidence"
            )
        try:
            snapshot = self.load_job_snapshot(job_id)
        except ProfileStorageError as exc:
            raise ProfileStateError(
                "test_required",
                "The referenced H3 profile test job snapshot is unavailable",
                details={"import_id": import_id, "job_id": job_id},
            ) from exc
        if (
            snapshot.eligible_workers != (worker,)
            or snapshot.profile_id != import_id
            or snapshot.workflow_sha256 != workflow_sha256
            or (
                boundary_sha256 is None
                and self.mapping_sha256(snapshot.mapping) != mapping_sha256
            )
            or (
                boundary_sha256 is not None
                and self.boundary_sha256(snapshot.mapping) != boundary_sha256
            )
        ):
            raise ProfileChangedError(
                "H3 profile test job snapshot does not match its evidence"
            )

        inspection = self._require_current_inspection(
            import_id, worker, workflow_sha256
        )
        snapshot_inspection = snapshot.worker_proofs[0].get("inspection", {})
        for field in ("metadata_sha256", "binding_generation"):
            if (
                snapshot_inspection.get(field) != inspection[field]
                or params.get(f"h3_profile_test_{field}") != inspection[field]
            ):
                raise ProfileChangedError(
                    "H3 profile test used an earlier worker binding or metadata inspection; a new test is required"
                )
        candidates = self._test_video_candidates(job)
        if not candidates:
            raise ProfileStateError(
                "test_required",
                "The referenced H3 profile test job has no mapped video output",
                details={"import_id": import_id, "job_id": job_id},
            )
        return job

    def _test_video_candidates(self, job: JobRecord) -> list[dict[str, Any]]:
        """Return durable test videos in worker-observed artifact order."""
        from app.core.jobs.store import job_dir

        candidates: list[dict[str, Any]] = []
        outputs = job.outputs or {}
        for index in range(len(outputs)):
            key = f"video_candidate_{index}"
            video = outputs.get(key)
            if video is None and len(outputs) == 1 and index == 0:
                video = outputs.get("video")
            if video is None:
                break
            filename = video.filename
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
                or Path(filename).suffix.lower()
                not in {".mp4", ".webm", ".mov", ".mkv"}
            ):
                continue
            output_path = self._safe_path(job_dir(job.id) / "outputs" / filename)
            expected_url = f"/api/files/jobs/{job.id}/outputs/{filename}"
            if (
                output_path.is_file()
                and output_path.stat().st_size > 0
                and video.path
                and Path(video.path).resolve() == output_path
                and video.url == expected_url
            ):
                candidates.append(
                    {
                        "artifact_index": index,
                        "key": key,
                        "filename": filename,
                        "url": video.url,
                    }
                )
        return candidates

    def install_profile(
        self,
        profile: H3WorkflowProfile,
        workflow: dict[str, Any],
        *,
        validation_record: dict[str, Any] | None = None,
        test_record: dict[str, Any] | None = None,
    ) -> None:
        """Persist one already-validated custom profile using only its safe ID."""
        profile_id = self._require_profile_id(profile.id)
        if not isinstance(workflow, dict):
            raise ProfileStorageError("Profile workflow must be a JSON object")
        self._assert_profile_boundary(workflow, profile.mapping)
        workflow_bytes = self._json_bytes(workflow)
        actual_hash = self._sha256(workflow_bytes)
        if profile.workflow_sha256 != actual_hash:
            raise ProfileChangedError(
                "Profile workflow hash does not match supplied workflow"
            )
        if (validation_record is None) != (test_record is None):
            raise ProfileStorageError(
                "Installed validation and test evidence must be supplied together"
            )
        directory = self._profile_dir(profile_id)
        proofs: list[dict[str, Any]] = []
        if validation_record is not None and test_record is not None:
            incoming = {**validation_record, "test": test_record}
            worker = self._validate_worker_proof(
                incoming,
                workflow_sha256=actual_hash,
                mapping_sha256=self.mapping_sha256(profile.mapping),
            )
            if self.profile_path(profile_id).exists():
                existing = self._resolve_custom(profile_id)
                if (
                    existing.workflow_sha256 != actual_hash
                    or existing.mapping != profile.mapping
                ):
                    raise ProfileChangedError(
                        "Cannot replace an installed profile with different graph or mapping bytes"
                    )
                proofs = [
                    proof
                    for proof in existing.worker_proofs
                    if self._binding_from_record(proof) != worker
                ]
            proofs.append(incoming)
            bindings = tuple(self._binding_from_record(proof) for proof in proofs)
            # A reused ID at a new URL is another exact proof, never an alias.
            profile = profile.model_copy(update={"eligible_workers": bindings})
            for proof in proofs:
                proof["profile_sha256"] = self._profile_sha256(profile)
        elif profile.eligible_workers:
            raise ProfileStorageError(
                "Worker eligibility requires matching validation and test evidence"
            )
        self._mkdir(directory)
        self._atomic_write_bytes(directory / _WORKFLOW_FILE, workflow_bytes)
        self._atomic_write_json(
            directory / _PROFILE_FILE, profile.model_dump(mode="json")
        )
        if proofs:
            self._atomic_write_json(
                directory / _VALIDATION_FILE, {**proofs[-1], "worker_proofs": proofs}
            )

    def _validate_worker_proof(
        self, proof: dict[str, Any], *, workflow_sha256: str, mapping_sha256: str
    ) -> H3WorkerBinding:
        worker = self._binding_from_record(proof)
        test = proof.get("test")
        inspection = proof.get("inspection")
        report = proof.get("report")
        comfy = proof.get("comfy")
        if (
            proof.get("valid") is not True
            or proof.get("contract_version") != 2
            or not isinstance(report, dict)
            or report.get("valid") is not True
            or not isinstance(comfy, dict)
            or comfy.get("valid") is not True
            or not isinstance(test, dict)
            or test.get("status") != "succeeded"
            or test.get("contract_version") != 2
            or not isinstance(test.get("job_id"), str)
            or not test["job_id"]
            or not isinstance(inspection, dict)
        ):
            raise ProfileStorageError(
                "Custom profile requires durable worker validation and test evidence"
            )
        if (
            self._binding_from_record(test) != worker
            or self._binding_from_record(inspection) != worker
        ):
            raise ProfileChangedError(
                "Installed profile inspection, validation, and test have different workers"
            )
        metadata_hash = inspection.get("metadata_sha256")
        if (
            not isinstance(metadata_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata_hash)
            or comfy.get("metadata_sha256") != metadata_hash
            or test.get("metadata_sha256") != metadata_hash
            or not isinstance(inspection.get("binding_generation"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", inspection["binding_generation"])
            or test.get("binding_generation") != inspection["binding_generation"]
        ):
            raise ProfileChangedError(
                "Installed validation no longer matches its inspected worker metadata"
            )
        if (
            proof.get("workflow_sha256") != workflow_sha256
            or proof.get("mapping_sha256") != mapping_sha256
            or inspection.get("workflow_sha256") != workflow_sha256
            or test.get("workflow_sha256") != workflow_sha256
            or test.get("mapping_sha256") != mapping_sha256
        ):
            raise ProfileChangedError(
                "Stored profile differs from its validation and test evidence"
            )
        return worker

    def select_profile(self, profile_id: str) -> None:
        """Atomically point future jobs at the requested verified profile."""
        profile_id = self._require_profile_id(profile_id)
        if profile_id == _BUILTIN_PROFILE_ID:
            resolved = self._resolve_builtin()
        else:
            resolved = self._resolve_custom(profile_id)
        self._mkdir(self.root)
        self._atomic_write_json(
            self.active_path,
            {
                "profile_id": resolved.profile_id,
                "workflow_sha256": resolved.workflow_sha256,
            },
        )

    def resolve_active(self) -> ResolvedH3Profile:
        """Honor a saved selection exactly; only a fresh store uses the builtin."""
        requested: object = "unreadable active pointer"
        try:
            if not self._safe_path(self.active_path).exists():
                return replace(
                    self._resolve_builtin(),
                    selection_source="initial_default",
                    selection_message="No workflow has been selected. The shipped Built-in Official H3 is the initial default.",
                )
            pointer = self._read_json(self.active_path)
            requested = pointer.get("profile_id")
            profile_id = self._require_profile_id(requested)
            expected_hash = pointer.get("workflow_sha256")
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise ProfileStorageError(
                    "Active profile pointer has an invalid workflow hash"
                )
            resolved = self.resolve_profile(profile_id)
            if resolved.workflow_sha256 != expected_hash:
                raise ProfileChangedError(
                    "Active profile hash no longer matches its pointer"
                )
            return resolved
        except (ProfileStorageError, ValidationError, OSError, TypeError) as exc:
            raise ProfileStateError(
                "selected_profile_unavailable",
                f"Selected H3 profile {requested!r} is unavailable: {exc}",
                details={"profile_id": requested},
            ) from exc

    def resolve_builtin(self) -> ResolvedH3Profile:
        """Resolve the packaged profile for setup/status responses."""
        return self._resolve_builtin()

    def resolve_profile(self, profile_id: str) -> ResolvedH3Profile:
        """Read verified installed profile details without changing selection."""
        profile_id = self._require_profile_id(profile_id)
        return (
            self._resolve_builtin()
            if profile_id == _BUILTIN_PROFILE_ID
            else self._resolve_custom(profile_id)
        )

    def snapshot_for_job(self, job: JobRecord) -> ResolvedH3Profile:
        """Atomically capture the currently resolved profile for one local H3 job."""
        from app.core.jobs.store import job_dir

        params = job.params or {}
        snapshot_dir = job_dir(job.id, project_id=job.project_id) / _JOB_SNAPSHOT_DIR
        identity_keys = {
            "h3_profile_id",
            "h3_profile_sha256",
            "h3_contract_version",
        }
        if self._safe_path(snapshot_dir).exists() or identity_keys.intersection(params):
            snapshot = self.load_job_snapshot(job.id)
            if (
                params.get("h3_eligible_workers")
                != [w.model_dump(mode="json") for w in snapshot.eligible_workers]
                or params.get("h3_profile_id") != snapshot.profile_id
                or params.get("h3_profile_sha256") != snapshot.workflow_sha256
                or params.get("h3_contract_version") != 2
            ):
                raise ProfileChangedError(
                    "Job profile snapshot identity does not match its job record"
                )
            return snapshot

        self._require_presubmit_job(job)
        resolved = self.resolve_active()
        if resolved.source == "builtin":
            workflow_path = Path(settings.workflows_dir) / "h3_ref2va.api.json"
            profile = H3WorkflowProfile(
                id=resolved.profile_id,
                workflow_sha256=resolved.workflow_sha256,
                mapping=resolved.mapping,
                status="active",
            )
            profile_bytes = self._json_bytes(profile.model_dump(mode="json"))
        else:
            workflow_path = self.workflow_path(resolved.profile_id)
            profile_path = self.profile_path(resolved.profile_id)
            try:
                profile_bytes = self._read_bytes(profile_path)
            except OSError as exc:
                raise ProfileStorageError(
                    "Could not read active profile while snapshotting the job"
                ) from exc
            try:
                source_profile = H3WorkflowProfile.model_validate(
                    self._parse_json(profile_bytes, profile_path.name)
                )
            except ValidationError as exc:
                raise ProfileStorageError(
                    "Active profile metadata changed while snapshotting the job"
                ) from exc
            if (
                source_profile.id != resolved.profile_id
                or source_profile.workflow_sha256 != resolved.workflow_sha256
                or source_profile.mapping != resolved.mapping
                or source_profile.eligible_workers != resolved.eligible_workers
            ):
                raise ProfileChangedError(
                    "Active profile metadata changed while snapshotting the job"
                )

        try:
            workflow_bytes = self._read_bytes(workflow_path)
        except OSError as exc:
            raise ProfileStorageError(
                "Could not read active workflow while snapshotting the job"
            ) from exc
        if self._sha256(workflow_bytes) != resolved.workflow_sha256:
            raise ProfileChangedError(
                "Active workflow changed while snapshotting the job"
            )

        self._atomic_write_bytes(snapshot_dir / _WORKFLOW_FILE, workflow_bytes)
        self._atomic_write_bytes(snapshot_dir / _PROFILE_FILE, profile_bytes)
        self._atomic_write_json(
            snapshot_dir / _PROOFS_FILE,
            {
                "workers": [
                    w.model_dump(mode="json") for w in resolved.eligible_workers
                ],
                "proofs": list(resolved.worker_proofs),
            },
        )
        job.params = dict(job.params or {})
        job.params.update(
            {
                "h3_eligible_workers": [
                    w.model_dump(mode="json") for w in resolved.eligible_workers
                ],
                "h3_profile_id": resolved.profile_id,
                "h3_profile_sha256": resolved.workflow_sha256,
                "h3_contract_version": 2,
            }
        )
        return resolved

    def snapshot_import_for_job(self, job: JobRecord) -> ResolvedH3Profile:
        """Capture one validated, unactivated import for its isolated test job."""
        from app.core.jobs.store import job_dir

        params = job.params or {}
        import_id = params.get("h3_profile_import_id")
        if not isinstance(import_id, str):
            raise ProfileStorageError("H3 profile test job has no import ID")
        worker = self.require_import_worker(import_id)
        self._require_job_worker(job, worker, durable=False)
        expected_workflow_sha256 = params.get("h3_profile_test_workflow_sha256")
        expected_mapping_sha256 = params.get("h3_profile_test_mapping_sha256")
        if not isinstance(expected_workflow_sha256, str) or not isinstance(
            expected_mapping_sha256, str
        ):
            raise ProfileStorageError("H3 profile test job has no captured identity")

        snapshot_dir = job_dir(job.id, project_id=job.project_id) / _JOB_SNAPSHOT_DIR
        identity_keys = {
            "h3_profile_id",
            "h3_profile_sha256",
            "h3_contract_version",
        }
        if self._safe_path(snapshot_dir).exists() or identity_keys.intersection(params):
            snapshot = self.load_job_snapshot(job.id)
            if (
                params.get("h3_profile_id") != import_id
                or params.get("h3_profile_sha256") != expected_workflow_sha256
                or params.get("h3_contract_version") != 2
                or snapshot.eligible_workers != (worker,)
                or params.get("h3_eligible_workers") != [worker.model_dump(mode="json")]
                or snapshot.profile_id != import_id
                or snapshot.workflow_sha256 != expected_workflow_sha256
                or self.mapping_sha256(snapshot.mapping) != expected_mapping_sha256
            ):
                raise ProfileChangedError(
                    "Test job profile snapshot identity does not match its job record"
                )
            inspection = self._require_current_inspection(
                import_id, worker, expected_workflow_sha256
            )
            self._capture_test_inspection(job, inspection)
            snapshot_inspection = snapshot.worker_proofs[0].get("inspection", {})
            if any(
                snapshot_inspection.get(field) != inspection[field]
                for field in ("binding_generation", "metadata_sha256")
            ):
                raise ProfileChangedError(
                    "Test snapshot belongs to an earlier worker binding or metadata inspection"
                )
            return snapshot

        directory = self._require_existing_import(import_id)
        workflow, workflow_sha256 = self._read_workflow(directory / _WORKFLOW_FILE)
        mapping = self.load_import_mapping(import_id)
        if mapping is None:
            raise ProfileStateError(
                "mapping_required",
                "A workflow mapping is required before testing",
                details={"import_id": import_id},
            )
        mapping_sha256 = self.mapping_sha256(mapping)
        if (
            workflow_sha256 != expected_workflow_sha256
            or mapping_sha256 != expected_mapping_sha256
        ):
            raise ProfileChangedError(
                "Imported workflow or mapping changed before test submission"
            )
        self._require_current_validation(
            directory,
            import_id=import_id,
            workflow_sha256=workflow_sha256,
            mapping_sha256=mapping_sha256,
        )
        inspection = self._require_current_inspection(
            import_id, worker, workflow_sha256
        )
        self._capture_test_inspection(job, inspection)
        params = job.params
        self._assert_profile_boundary(workflow, mapping)
        profile = H3WorkflowProfile(
            id=import_id,
            workflow_sha256=workflow_sha256,
            mapping=mapping,
            status="validated",
            eligible_workers=(worker,),
        )

        if self.import_identity(import_id) != (workflow_sha256, mapping_sha256):
            raise ProfileChangedError(
                "Imported workflow or mapping changed before test snapshot"
            )
        self._require_presubmit_job(job)
        self._atomic_write_bytes(
            snapshot_dir / _WORKFLOW_FILE,
            self._json_bytes(workflow),
        )
        self._atomic_write_json(
            snapshot_dir / _PROFILE_FILE,
            profile.model_dump(mode="json"),
        )
        self._atomic_write_json(
            snapshot_dir / _PROOFS_FILE,
            {
                "workers": [worker.model_dump(mode="json")],
                "proofs": [
                    self._require_current_validation(
                        directory,
                        import_id=import_id,
                        workflow_sha256=workflow_sha256,
                        mapping_sha256=mapping_sha256,
                    )
                ],
            },
        )
        job.params = dict(params)
        job.params.update(
            {
                "h3_eligible_workers": [worker.model_dump(mode="json")],
                "h3_profile_id": import_id,
                "h3_profile_sha256": workflow_sha256,
                "h3_contract_version": 2,
            }
        )
        return ResolvedH3Profile(
            profile_id=import_id,
            workflow=workflow,
            mapping=mapping,
            workflow_sha256=workflow_sha256,
            source="custom",
            eligible_workers=(worker,),
        )

    def load_job_snapshot(self, job_id: str) -> ResolvedH3Profile:
        """Load and verify the immutable profile snapshot captured for a job."""
        from app.core.jobs.store import job_dir

        snapshot_dir = job_dir(job_id) / _JOB_SNAPSHOT_DIR
        profile_path = snapshot_dir / _PROFILE_FILE
        workflow_path = snapshot_dir / _WORKFLOW_FILE
        profile_data = self._read_json(profile_path)
        try:
            profile = H3WorkflowProfile.model_validate(profile_data)
        except ValidationError as exc:
            raise ProfileStorageError(
                "Job profile snapshot metadata is invalid"
            ) from exc
        workflow, workflow_hash = self._read_workflow(workflow_path)
        if profile.workflow_sha256 != workflow_hash:
            raise ProfileChangedError(
                "Job workflow profile snapshot hash does not match"
            )
        self._assert_profile_boundary(workflow, profile.mapping)
        proof_record = self._optional_record(snapshot_dir / _PROOFS_FILE)
        if profile.id != _BUILTIN_PROFILE_ID and (
            not profile.eligible_workers
            or proof_record is None
            or proof_record.get("workers")
            != [w.model_dump(mode="json") for w in profile.eligible_workers]
        ):
            raise ProfileStorageError(
                "Custom job snapshot has no matching worker eligibility evidence"
            )
        if profile.id != _BUILTIN_PROFILE_ID:
            proofs = proof_record.get("proofs")
            if not isinstance(proofs, list) or len(proofs) != len(
                profile.eligible_workers
            ):
                raise ProfileStorageError(
                    "Custom job snapshot has incomplete worker evidence"
                )
            for worker, proof in zip(profile.eligible_workers, proofs, strict=True):
                if (
                    not isinstance(proof, dict)
                    or self._binding_from_record(proof) != worker
                ):
                    raise ProfileChangedError(
                        "Job snapshot worker proof does not match its eligibility"
                    )
                if profile.status == "validated":
                    inspection = proof.get("inspection")
                    if (
                        proof.get("valid") is not True
                        or proof.get("workflow_sha256") != workflow_hash
                        or proof.get("mapping_sha256")
                        != self.mapping_sha256(profile.mapping)
                        or not isinstance(inspection, dict)
                        or self._binding_from_record(inspection) != worker
                    ):
                        raise ProfileChangedError(
                            "Test job snapshot validation identity is invalid"
                        )
                else:
                    self._validate_worker_proof(
                        proof,
                        workflow_sha256=workflow_hash,
                        mapping_sha256=self.mapping_sha256(profile.mapping),
                    )
                    if proof.get("profile_sha256") != self._profile_sha256(profile):
                        raise ProfileChangedError(
                            "Job snapshot profile changed after worker verification"
                        )
        return ResolvedH3Profile(
            eligible_workers=profile.eligible_workers,
            worker_proofs=tuple((proof_record or {}).get("proofs", [])),
            profile_id=profile.id,
            workflow=workflow,
            mapping=profile.mapping,
            workflow_sha256=workflow_hash,
            source="builtin" if profile.id == _BUILTIN_PROFILE_ID else "custom",
        )

    def _resolve_builtin(self) -> ResolvedH3Profile:
        path = Path(settings.workflows_dir) / "h3_ref2va.api.json"
        workflow, workflow_hash = self._read_workflow(path)
        return ResolvedH3Profile(
            profile_id=_BUILTIN_PROFILE_ID,
            workflow=workflow,
            mapping=_OFFICIAL_MAPPING,
            workflow_sha256=workflow_hash,
            source="builtin",
            display_name="Built-in Official H3",
        )

    def _resolve_custom(self, profile_id: str) -> ResolvedH3Profile:
        profile_id = self._require_profile_id(profile_id)
        profile_path = self.profile_path(profile_id)
        workflow_path = self.workflow_path(profile_id)
        profile_data = self._read_json(profile_path)
        try:
            profile = H3WorkflowProfile.model_validate(profile_data)
        except ValidationError as exc:
            raise ProfileStorageError("Stored profile metadata is invalid") from exc
        if profile.id != profile_id:
            raise ProfileStorageError("Stored profile ID does not match its directory")
        if profile.status not in {"tested", "active"}:
            raise ProfileStorageError(
                "Custom profile must be tested or active before selection"
            )
        workflow, workflow_hash = self._read_workflow(workflow_path)
        if profile.workflow_sha256 != workflow_hash:
            raise ProfileChangedError("Stored workflow differs from the profile hash")
        mapping_hash = self.mapping_sha256(profile.mapping)
        evidence = self._optional_record(
            self._profile_dir(profile_id) / _VALIDATION_FILE
        )
        if evidence is None:
            raise ProfileStorageError(
                "Custom profile requires durable validation and test evidence"
            )
        proofs = evidence.get("worker_proofs")
        if (
            not isinstance(proofs, list)
            or not proofs
            or not all(isinstance(proof, dict) for proof in proofs)
        ):
            raise ProfileStorageError(
                "Custom profile requires durable per-worker validation and test evidence"
            )
        workers = tuple(
            self._validate_worker_proof(
                proof, workflow_sha256=workflow_hash, mapping_sha256=mapping_hash
            )
            for proof in proofs
        )
        if workers != profile.eligible_workers or len(
            set((worker.worker_id, worker.worker_url) for worker in workers)
        ) != len(workers):
            raise ProfileChangedError(
                "Stored profile worker eligibility differs from its verified evidence"
            )
        if any(
            proof.get("profile_sha256") != self._profile_sha256(profile)
            for proof in proofs
        ):
            raise ProfileChangedError(
                "Stored profile differs from its validation and test evidence"
            )
        self._assert_profile_boundary(workflow, profile.mapping)
        return ResolvedH3Profile(
            eligible_workers=workers,
            worker_proofs=tuple(proofs),
            profile_id=profile.id,
            workflow=workflow,
            mapping=profile.mapping,
            workflow_sha256=workflow_hash,
            source="custom",
            display_name=self._display_name(evidence),
            validated_at=evidence.get("validated_at")
            if isinstance(evidence.get("validated_at"), str)
            else None,
        )

    @staticmethod
    def _require_presubmit_job(job: JobRecord) -> None:
        from app.core.schemas import JobStatus

        if (
            job.status not in {JobStatus.queued, JobStatus.uploading}
            or job.comfy_prompt_id
        ):
            raise ProfileStorageError(
                "H3 execution job is missing its original workflow snapshot; active profile substitution is forbidden"
            )

    @staticmethod
    def _require_job_worker(
        job: JobRecord, worker: H3WorkerBinding, *, durable: bool
    ) -> None:
        params = job.params or {}
        if (
            params.get("worker_id") != worker.worker_id
            or params.get("worker_url") != worker.worker_url
        ):
            raise ProfileChangedError(
                "H3 test job requested worker does not match the import worker"
            )
        if durable or job.worker_id is not None or job.worker_url is not None:
            if job.worker_id != worker.worker_id or job.worker_url != worker.worker_url:
                raise ProfileChangedError(
                    "H3 test job durable worker pin does not match the import worker"
                )

    @staticmethod
    def _display_name(metadata: dict[str, Any] | None) -> str:
        value = (metadata or {}).get("display_name")
        if not isinstance(value, str):
            return "Custom H3 workflow"
        return (
            re.sub(r"[\x00-\x1f\x7f\s]+", " ", value).strip()[:120]
            or "Custom H3 workflow"
        )

    def _profile_dir(self, profile_id: str) -> Path:
        return self._safe_path(self.profiles_dir / self._require_profile_id(profile_id))

    def _import_dir(self, import_id: str) -> Path:
        if not isinstance(import_id, str) or not _IMPORT_ID_RE.fullmatch(import_id):
            raise ProfileStorageError("Invalid workflow import ID")
        return self._safe_path(self.imports_dir / import_id)

    @staticmethod
    def _require_profile_id(profile_id: object) -> str:
        if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ProfileStorageError("Invalid workflow profile ID")
        return profile_id

    @staticmethod
    def _sha256(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _read_workflow(self, path: Path) -> tuple[dict[str, Any], str]:
        try:
            raw = self._read_bytes(path)
        except OSError as exc:
            raise ProfileStorageError(
                f"Could not read workflow file: {path.name}"
            ) from exc
        return self._parse_json(raw, path.name), self._sha256(raw)

    @staticmethod
    def _assert_profile_boundary(
        workflow: dict[str, Any], mapping: H3BoundaryMapping
    ) -> None:
        from .validator import validate_h3_contract

        report = validate_h3_contract(workflow, mapping)
        if not report.valid:
            detail = "; ".join(issue.message for issue in report.issues)
            raise ProfileStorageError(
                f"Custom graph does not satisfy its confirmed H3 boundary: {detail}"
            )

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            raw = self._read_bytes(path)
        except OSError as exc:
            raise ProfileStorageError(
                f"Could not read profile file: {path.name}"
            ) from exc
        return self._parse_json(raw, path.name)

    @staticmethod
    def _parse_json(raw: bytes, name: str) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileStorageError(f"Invalid JSON in {name}") from exc
        if not isinstance(value, dict):
            raise ProfileStorageError(f"JSON object required in {name}")
        return value

    def _atomic_write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_write_bytes(path, self._json_bytes(value))

    def _safe_path(self, path: Path, *, write: bool = False) -> Path:
        """Check containment and every existing component without following links."""
        path = Path(os.path.abspath(path))
        roots = [self.root, settings.jobs_dir, settings.projects_dir]
        if not write:
            roots.append(settings.workflows_dir)
        allowed = [Path(os.path.abspath(root)) for root in roots]
        if not any(path.is_relative_to(root) for root in allowed):
            raise ProfileStorageError(
                "Workflow profile path is outside its storage roots"
            )
        try:
            for component in (*reversed(path.parents), path):
                try:
                    info = os.lstat(component)
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)
                ):
                    raise ProfileStorageError(
                        "Workflow profile paths cannot contain links or reparse points"
                    )
            if not any(path.resolve().is_relative_to(root) for root in allowed):
                raise ProfileStorageError(
                    "Workflow profile path escapes its storage root"
                )
        except (OSError, RuntimeError) as exc:
            raise ProfileStorageError(
                "Could not verify workflow profile storage path"
            ) from exc
        return path

    def _read_bytes(self, path: Path) -> bytes:
        path = self._safe_path(path)
        raw = path.read_bytes()
        self._safe_path(path)
        return raw

    def _mkdir(self, path: Path) -> None:
        self._safe_path(path, write=True).mkdir(parents=True, exist_ok=True)
        self._safe_path(path, write=True)

    def _atomic_write_bytes(self, path: Path, value: bytes) -> None:
        path = self._safe_path(path, write=True)
        self._mkdir(path.parent)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as temporary:
                temp_name = temporary.name
                temporary.write(value)
                temporary.flush()
                os.fsync(temporary.fileno())
            self._safe_path(Path(temp_name), write=True)
            self._safe_path(path, write=True)
            os.replace(temp_name, path)
        except OSError as exc:
            raise ProfileStorageError(
                f"Could not atomically write {path.name}"
            ) from exc
        finally:
            if temp_name:
                try:
                    self._safe_path(Path(temp_name), write=True).unlink(missing_ok=True)
                except (OSError, ProfileStorageError):
                    pass


def resolve_active_h3_profile() -> ResolvedH3Profile:
    """Resolve the active H3 profile for a newly submitted local H3 job."""
    return H3ProfileStore().resolve_active()


def snapshot_profile_for_job(job: JobRecord) -> ResolvedH3Profile:
    """Capture the active H3 profile before a local job enters the queue."""
    return H3ProfileStore().snapshot_for_job(job)


def load_job_profile_snapshot(job_id: str) -> ResolvedH3Profile:
    """Resolve a job's captured H3 profile without consulting the active pointer."""
    return H3ProfileStore().load_job_snapshot(job_id)
