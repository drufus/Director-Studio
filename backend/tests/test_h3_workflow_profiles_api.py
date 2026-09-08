"""HTTP coverage for deterministic output-first H3 workflow setup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.comfy import ComfyError
from app.core.comfy.client import ComfyClient
from app.core.comfy.workers import WorkerRegistry, parse_workers
from app.main import create_app
from app.workflow_profiles.h3 import H3ProfileStore, H3WorkerBinding, ProfileStateError


WORKER_URL = "http://remote-worker:8188"
WORKER = {"worker_id": "beastviii", "worker_url": WORKER_URL}
WORKER_QUERY = urlencode(WORKER)


@pytest.fixture
def sample_api_json() -> bytes:
    return (settings.workflows_dir / "h3_ref2va.api.json").read_bytes()


@pytest.fixture
def profile_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "workflow_profiles_dir", tmp_path / "profiles")
    monkeypatch.setattr(settings, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")

    class MetadataClient:
        base_url = WORKER_URL

        async def health(self):
            return {"system": {}, "devices": []}

        async def get_object_info(self):
            return {
                "MiniMaxH3ReferenceToVideo": {
                    "display_name": "MiniMax H3 Ref2AV",
                    "output": ["CONDITIONING"],
                    "output_node": False,
                },
                "SaveVideo": {
                    "display_name": "Save Video",
                    "output": ["VIDEO"],
                    "output_node": True,
                },
                "VHS_VideoCombine": {
                    "display_name": "Video Combine",
                    "output": ["VHS_FILENAMES"],
                    "output_node": True,
                },
            }

    class ValidatingClient(MetadataClient):
        async def validate_workflow(self, graph, *, object_info):
            assert any(
                node.get("class_type") == "MiniMaxH3ReferenceToVideo"
                for node in graph.values()
            )
            return {"valid": True, "error_count": 0, "warnings": []}

        async def aclose(self):
            return None

    class Registry:
        def client_for(self, worker_id):
            if worker_id != "beastviii":
                raise ComfyError(f"Unknown render worker: {worker_id}")
            return ValidatingClient()

    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", Registry)
    with TestClient(create_app()) as client:
        yield client


def _import(client: TestClient, workflow: bytes, name: str = "custom.api.json") -> str:
    response = client.post(
        "/api/workflow-profiles/h3/imports",
        files={"workflow": (name, workflow, "application/json")},
    )
    assert response.status_code == 201, response.text
    import_id = response.json()["import_id"]
    bound = client.put(f"/api/workflow-profiles/h3/imports/{import_id}/worker", json=WORKER)
    assert bound.status_code == 200, bound.text
    return import_id


def _select_output_and_mapping(client: TestClient, import_id: str, node_id="92"):
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    selected = client.put(f"{base}/output?{WORKER_QUERY}", json={"node_id": node_id})
    assert selected.status_code == 200, selected.text
    mapping = selected.json()["mapping"]
    saved = client.put(f"{base}/mapping?{WORKER_QUERY}", json=mapping)
    assert saved.status_code == 200, saved.text
    return mapping


def test_import_lists_named_output_then_reverse_discovers_h3(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    import_id = _import(profile_client, sample_api_json, "Cinema H3.api.json")
    base = f"/api/workflow-profiles/h3/imports/{import_id}"

    analysis = profile_client.get(f"{base}/analysis?{WORKER_QUERY}")
    assert analysis.status_code == 200
    output = analysis.json()["output_candidates"][0]
    assert output["node_id"] == "92"
    assert output["display_name"] == "Save Video"
    assert output["class_type"] == "SaveVideo"
    assert analysis.json()["h3_candidates"] == []

    selected = profile_client.put(f"{base}/output?{WORKER_QUERY}", json={"node_id": "92"})
    assert selected.status_code == 200
    body = selected.json()
    assert body["h3_candidates"][0]["node_id"] == "136"
    assert body["mapping"]["inputs"]["h3_node_id"] == "136"
    assert body["mapping"]["output"] == {
        "node_id": "92",
        "artifact_index": None,
    }


def test_mapping_proposal_route_is_removed(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    import_id = _import(profile_client, sample_api_json)

    response = profile_client.post(
        f"/api/workflow-profiles/h3/imports/{import_id}/propose-mapping"
    )

    assert response.status_code in {404, 405}


def test_output_change_invalidates_mapping_and_lifecycle(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    graph = json.loads(sample_api_json)
    graph["999"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": graph["92"]["inputs"]["video"],
            "filename_prefix": "alternate",
        },
        "_meta": {"title": "Alternate final video"},
    }
    import_id = _import(profile_client, json.dumps(graph).encode())
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    _select_output_and_mapping(profile_client, import_id)
    assert profile_client.get(f"{base}/analysis?{WORKER_QUERY}").json()["lifecycle"]["status"] == "mapped"

    changed = profile_client.put(f"{base}/output?{WORKER_QUERY}", json={"node_id": "999"})

    assert changed.status_code == 200
    assert changed.json()["mapping"] is not None
    assert changed.json()["mapping"]["output"]["node_id"] == "999"
    assert changed.json()["lifecycle"]["status"] == "draft"
    assert H3ProfileStore().load_import_mapping(import_id) is None


def test_rejects_nonterminal_or_unknown_output_selection(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    import_id = _import(profile_client, sample_api_json)

    response = profile_client.put(
        f"/api/workflow-profiles/h3/imports/{import_id}/output?{WORKER_QUERY}",
        json={"node_id": "136"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "output_selection_error"
    assert "136" in response.json()["message"]


def test_confirmed_boundary_validates_without_agent(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)

    response = profile_client.post(
        f"/api/workflow-profiles/h3/imports/{import_id}/validate?{WORKER_QUERY}"
    )

    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True
    assert response.json()["lifecycle"]["status"] == "validated"


def test_mapping_must_match_selected_output(
    profile_client: TestClient, sample_api_json: bytes
) -> None:
    import_id = _import(profile_client, sample_api_json)
    mapping = profile_client.put(
        f"/api/workflow-profiles/h3/imports/{import_id}/output?{WORKER_QUERY}",
        json={"node_id": "92"},
    ).json()["mapping"]
    mapping["output"]["node_id"] = "136"

    response = profile_client.put(
        f"/api/workflow-profiles/h3/imports/{import_id}/mapping?{WORKER_QUERY}", json=mapping
    )

    assert response.status_code == 422
    assert response.json()["code"] == "input_selection_error"


def test_import_reports_each_malformed_node_by_name(profile_client: TestClient) -> None:
    graph = {
        "58": {"inputs": {}, "_meta": {"title": "Broken Loader"}},
        "140": [],
    }

    response = profile_client.post(
        "/api/workflow-profiles/h3/imports",
        files={
            "workflow": ("bad.json", json.dumps(graph).encode(), "application/json")
        },
    )

    assert response.status_code == 400
    issues = response.json()["details"]["issues"]
    assert {(item["node_id"], item["node_name"]) for item in issues} == {
        ("58", "Broken Loader"),
        ("140", "Node 140"),
    }
    assert not H3ProfileStore().imports_dir.exists()


def test_routes_reject_non_opaque_ids_with_structured_errors(
    profile_client: TestClient,
) -> None:
    response = profile_client.get(
        f"/api/workflow-profiles/h3/imports/..%2Foutside/analysis?{WORKER_QUERY}"
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_import_id"


def test_profile_listing_starts_on_builtin_official(profile_client: TestClient) -> None:
    response = profile_client.get("/api/workflow-profiles/h3")

    assert response.status_code == 200
    body = response.json()
    assert body["active"]["profile_id"] == "builtin-official-h3"
    assert body["active"]["contract_version"] == 2


def test_analysis_reports_selected_worker_metadata_failure_without_fallback(
    profile_client: TestClient,
    sample_api_json: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = _import(profile_client, sample_api_json)

    class OfflineMetadataClient:
        base_url = WORKER_URL

        async def get_object_info(self):
            raise ComfyError("Worker beastviii object_info unavailable: offline")

    class OfflineRegistry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return OfflineMetadataClient()

    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", OfflineRegistry)

    response = profile_client.get(
        f"/api/workflow-profiles/h3/imports/{import_id}/analysis?{WORKER_QUERY}"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Worker beastviii object_info unavailable: offline"
    assert "output_candidates" not in response.json()


@pytest.mark.parametrize("method,endpoint,body", [
    ("GET", "analysis", None), ("PUT", "output", {"node_id": "92"}),
    ("POST", "validate", None),
])
def test_worker_metadata_operations_require_explicit_worker(
    profile_client, sample_api_json, method, endpoint, body
):
    import_id = _import(profile_client, sample_api_json)
    response = profile_client.request(
        method, f"/api/workflow-profiles/h3/imports/{import_id}/{endpoint}", json=body
    )
    assert response.status_code == 422
    missing = {tuple(item["loc"]) for item in response.json()["detail"]}
    assert {("query", "worker_id"), ("query", "worker_url")} <= missing


@pytest.mark.parametrize("method,endpoint,body,status", [
    ("GET", "analysis", None, 409),
    ("PUT", "output", {"node_id": "92"}, 409),
    ("POST", "validate", None, 409),
])
def test_unknown_worker_cannot_use_another_workers_metadata(
    profile_client, sample_api_json, method, endpoint, body, status
):
    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    response = profile_client.request(
        method,
        f"/api/workflow-profiles/h3/imports/{import_id}/{endpoint}?worker_id=unknown-worker&worker_url={WORKER_URL}",
        json=body,
    )
    assert response.status_code == status
    assert "Unknown render worker: unknown-worker" in response.text
    assert H3ProfileStore().import_lifecycle(import_id)["status"] == "mapped"


@pytest.mark.parametrize("endpoint,method,body", [
    ("analysis", "GET", None),
    ("output", "PUT", {"node_id": "92"}),
    ("mapping", "PUT", "mapping"),
    ("validate", "POST", None),
    ("test-output", "PUT", {"artifact_index": 0}),
    ("activate", "POST", None),
])
def test_stale_worker_endpoint_rejected_before_network(
    profile_client, sample_api_json, monkeypatch, endpoint, method, body
):
    import_id = _import(profile_client, sample_api_json)
    mapping = _select_output_and_mapping(profile_client, import_id)
    requests = []

    class ChangedClient:
        base_url = "http://changed-worker:8188"

        async def get_object_info(self):
            requests.append("metadata")
            raise AssertionError("Stale endpoint must fail before a worker request")

    class ChangedRegistry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return ChangedClient()

    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", ChangedRegistry)
    response = profile_client.request(
        method,
        f"/api/workflow-profiles/h3/imports/{import_id}/{endpoint}?{WORKER_QUERY}",
        json=mapping if body == "mapping" else body,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "worker_changed"
    assert response.json()["details"]["expected_url"] == WORKER_URL
    assert requests == []


def test_explicit_worker_rebind_and_reload_invalidates_validation(
    profile_client, sample_api_json, monkeypatch
):
    from app.api import h3_workflow_profiles as api

    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    validated = profile_client.post(f"{base}/validate?{WORKER_QUERY}")
    assert validated.status_code == 200, validated.text
    original_factory = api.get_worker_registry
    other = {"worker_id": "worker-b", "worker_url": "http://worker-b:8188"}

    class Registry:
        def client_for(self, worker_id):
            client = original_factory().client_for("beastviii")
            client.base_url = other["worker_url"] if worker_id == "worker-b" else WORKER_URL
            return client

    monkeypatch.setattr(api, "get_worker_registry", Registry)
    changed = profile_client.put(f"{base}/worker", json=other)
    assert changed.status_code == 200, changed.text
    assert changed.json()["lifecycle"]["validated_at"] is None
    reloaded = profile_client.get(f"{base}/analysis", params=other)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["lifecycle"]["worker"] == other
    assert reloaded.json()["lifecycle"]["status"] == "mapped"
    assert reloaded.json()["lifecycle"]["validated_at"] is None
    stale = profile_client.post(f"{base}/validate?{WORKER_QUERY}")
    assert stale.status_code == 409
    assert stale.json()["code"] == "worker_mismatch"


@pytest.mark.parametrize("operation", ["analysis", "validate"])
def test_inflight_metadata_response_cannot_authorize_a_rebound_worker(
    profile_client, sample_api_json, monkeypatch, operation
):
    from app.api import h3_workflow_profiles as api

    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    original_client = api.get_worker_registry().client_for("beastviii")
    other = H3WorkerBinding(worker_id="worker-b", worker_url="http://worker-b:8188")

    class RebindingClient:
        base_url = WORKER_URL

        async def get_object_info(self):
            metadata = await original_client.get_object_info()
            H3ProfileStore().bind_import_worker(import_id, other)
            return metadata

        async def validate_workflow(self, graph, *, object_info):
            return await original_client.validate_workflow(graph, object_info=object_info)

    class Registry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return RebindingClient()

    monkeypatch.setattr(api, "get_worker_registry", Registry)
    response = profile_client.request(
        "GET" if operation == "analysis" else "POST",
        f"/api/workflow-profiles/h3/imports/{import_id}/{operation}?{WORKER_QUERY}",
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "worker_mismatch"
    lifecycle = H3ProfileStore().import_lifecycle(import_id)
    assert lifecycle["worker"] == other.model_dump(mode="json")
    assert lifecycle["validated_at"] is None
    assert lifecycle["status"] == "mapped"


@pytest.mark.parametrize("operation,rebind_during", [
    ("analysis", "metadata"),
    ("validate", "metadata"),
    ("validate", "validation"),
])
def test_switching_worker_away_and_back_rejects_the_original_inflight_operation(
    profile_client, sample_api_json, monkeypatch, operation, rebind_during
):
    from app.api import h3_workflow_profiles as api

    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    original_client = api.get_worker_registry().client_for("beastviii")
    original_generation = H3ProfileStore().import_binding_generation(import_id)

    def change_worker_and_restore_original():
        store = H3ProfileStore()
        store.bind_import_worker(import_id, H3WorkerBinding(
            worker_id="worker-b", worker_url="http://worker-b:8188"
        ))
        store.bind_import_worker(import_id, H3WorkerBinding(**WORKER))

    class RebindingClient:
        base_url = WORKER_URL

        async def get_object_info(self):
            metadata = await original_client.get_object_info()
            if rebind_during == "metadata":
                change_worker_and_restore_original()
            return metadata

        async def validate_workflow(self, graph, *, object_info):
            result = await original_client.validate_workflow(graph, object_info=object_info)
            if rebind_during == "validation":
                change_worker_and_restore_original()
            return result

    class Registry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return RebindingClient()

    monkeypatch.setattr(api, "get_worker_registry", Registry)
    response = profile_client.request(
        "GET" if operation == "analysis" else "POST",
        f"/api/workflow-profiles/h3/imports/{import_id}/{operation}?{WORKER_QUERY}",
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "profile_changed"
    assert "binding changed" in response.json()["message"]
    store = H3ProfileStore()
    assert store.import_binding_generation(import_id) != original_generation
    lifecycle = store.import_lifecycle(import_id)
    assert lifecycle["worker"] == WORKER
    assert lifecycle["inspected_at"] is None
    assert lifecycle["validated_at"] is None
    assert lifecycle["status"] == "mapped"


@pytest.mark.parametrize("dependency_error", [False, True])
def test_revalidation_observing_changed_worker_metadata_invalidates_old_proof(
    profile_client, sample_api_json, monkeypatch, dependency_error
):
    from app.api import h3_workflow_profiles as api

    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    assert profile_client.post(f"{base}/validate?{WORKER_QUERY}").status_code == 200
    original_client = api.get_worker_registry().client_for("beastviii")
    old_digest = H3ProfileStore().import_lifecycle(import_id)["metadata_sha256"]

    class ChangedMetadataClient:
        base_url = WORKER_URL

        async def get_object_info(self):
            metadata = await original_client.get_object_info()
            metadata["SaveVideo"]["output"] = ["CHANGED_VIDEO_CONTRACT"]
            return metadata

        async def validate_workflow(self, graph, *, object_info):
            if dependency_error:
                raise ComfyError("Worker beastviii SaveVideo dependency is no longer compatible")
            return await original_client.validate_workflow(graph, object_info=object_info)

    class Registry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return ChangedMetadataClient()

    monkeypatch.setattr(api, "get_worker_registry", Registry)
    response = profile_client.post(f"{base}/validate?{WORKER_QUERY}")
    lifecycle = H3ProfileStore().import_lifecycle(import_id)
    assert lifecycle["metadata_sha256"] != old_digest
    if dependency_error:
        assert response.status_code == 422, response.text
        assert lifecycle["validated_at"] is None
        with pytest.raises(ProfileStateError):
            H3ProfileStore().testable_import_identity(import_id)
    else:
        assert response.status_code == 200, response.text
        assert response.json()["comfy"]["metadata_sha256"] == lifecycle["metadata_sha256"]
        assert lifecycle["validated_at"] is not None


def test_clearing_worker_requires_inspection_and_validation_again(
    profile_client, sample_api_json
):
    import_id = _import(profile_client, sample_api_json)
    _select_output_and_mapping(profile_client, import_id)
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    assert profile_client.post(f"{base}/validate?{WORKER_QUERY}").status_code == 200
    cleared = profile_client.put(f"{base}/worker", json={"worker_id": None, "worker_url": None})
    assert cleared.status_code == 200
    assert cleared.json()["worker"] is None
    assert cleared.json()["lifecycle"]["validated_at"] is None
    blocked = profile_client.post(f"{base}/validate?{WORKER_QUERY}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "worker_required"


@pytest.mark.parametrize("url", [
    "http://100.88.79.40:8188",
    "https://user:private-value@worker.test",
    "http://worker.test?api_key=private-value",
    "file:///tmp/worker",
])
@pytest.mark.parametrize("operation", ["worker", "analysis"])
def test_invalid_or_forbidden_worker_url_is_structured_and_never_contacts_registry(
    profile_client, sample_api_json, monkeypatch, url, operation
):
    import_id = _import(profile_client, sample_api_json)

    def reject_registry():
        raise AssertionError("Invalid origin must be rejected before resolving a worker")

    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", reject_registry)
    fields = {"worker_id": "beastviii", "worker_url": url}
    base = f"/api/workflow-profiles/h3/imports/{import_id}"
    if operation == "worker":
        response = profile_client.put(f"{base}/worker", json=fields)
    else:
        response = profile_client.get(f"{base}/analysis", params=fields)
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["code"] == "worker_unavailable"
    assert "private-value" not in response.text


def test_missing_selected_profile_is_visible_until_explicit_builtin_recovery(profile_client):
    store = H3ProfileStore()
    store.root.mkdir(parents=True, exist_ok=True)
    pointer = {"profile_id": "missing-custom", "workflow_sha256": "a" * 64}
    store.active_path.write_text(json.dumps(pointer), encoding="utf-8")
    response = profile_client.get("/api/workflow-profiles/h3")
    assert response.status_code == 200
    assert response.json()["active"] is None
    assert response.json()["active_error"]["code"] == "selected_profile_unavailable"
    assert "missing-custom" in response.json()["active_error"]["message"]
    assert response.json()["selected_profile_id"] == "missing-custom"
    assert json.loads(store.active_path.read_text()) == pointer
    recovery = profile_client.post("/api/workflow-profiles/h3/select", json={"profile_id": "builtin-official-h3"})
    assert recovery.status_code == 200, recovery.text
    assert recovery.json()["active"]["selection_source"] == "explicit"
    assert recovery.json()["active"]["profile_id"] == "builtin-official-h3"
    assert profile_client.get("/api/workflow-profiles/h3").json()["active_error"] is None


@pytest.mark.parametrize("offline", [False, True])
def test_activation_checks_live_worker_health_without_rendering_or_freeing_memory(
    profile_client, monkeypatch, offline
):
    from test_h3_profile_test_run import _import_ready_profile, _record_durable_success

    store = H3ProfileStore()
    import_id = _import_ready_profile(store)
    _record_durable_success(store, import_id)
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        assert request.url.path == "/system_stats"
        if offline:
            raise httpx.ConnectError("Connection refused during activation", request=request)
        return httpx.Response(200, json={"system": {}, "devices": []})

    def factory(url, **kwargs):
        return ComfyClient(url, transport=httpx.MockTransport(handler), **kwargs)

    registry = WorkerRegistry(parse_workers(f"beastviii={WORKER_URL}"), client_factory=factory)
    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", lambda: registry)
    response = profile_client.post(
        f"/api/workflow-profiles/h3/imports/{import_id}/activate?{WORKER_QUERY}"
    )
    assert requests == [("GET", "/system_stats")]
    if offline:
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "worker_unavailable"
        assert "Connection refused during activation" in response.json()["message"]
        assert "beastviii" in response.json()["message"]
        states = asyncio.run(registry.status(refresh=False))
        assert states[0]["status"] == "down"
        assert store.resolve_active().source == "builtin"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["active"]["source"] == "custom"
        assert response.json()["active"]["eligible_workers"] == [WORKER]
        assert store.resolve_active().profile_id == response.json()["profile_id"]


@pytest.mark.parametrize("restore_original", [False, True])
def test_activation_rechecks_binding_after_pending_health_response(
    profile_client, sample_api_json, monkeypatch, restore_original
):
    import_id = _import(profile_client, sample_api_json)
    original_generation = H3ProfileStore().import_binding_generation(import_id)

    class RebindingClient:
        base_url = WORKER_URL

        async def health(self):
            store = H3ProfileStore()
            store.bind_import_worker(import_id, H3WorkerBinding(
                worker_id="worker-b", worker_url="http://worker-b:8188"
            ))
            if restore_original:
                store.bind_import_worker(import_id, H3WorkerBinding(**WORKER))
            return {"system": {}, "devices": []}

    class Registry:
        def client_for(self, worker_id):
            assert worker_id == "beastviii"
            return RebindingClient()

    def reject_activation(*args):
        raise AssertionError("A stale health result must fail before profile activation")

    monkeypatch.setattr("app.api.h3_workflow_profiles.get_worker_registry", Registry)
    monkeypatch.setattr(H3ProfileStore, "activate_import", reject_activation)
    response = profile_client.post(
        f"/api/workflow-profiles/h3/imports/{import_id}/activate?{WORKER_QUERY}"
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == ("profile_changed" if restore_original else "worker_mismatch")
    if restore_original:
        assert "binding changed during activation" in response.json()["message"]
    assert H3ProfileStore().import_binding_generation(import_id) != original_generation
    assert H3ProfileStore().resolve_active().source == "builtin"
