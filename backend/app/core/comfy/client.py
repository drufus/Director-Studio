"""HTTP-only ComfyUI transport. Every operation stays on one explicit endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import mimetypes
import re
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ...config import settings


class ComfyError(RuntimeError):
    pass


class ComfySubmissionError(ComfyError):
    """A rejected partial submission still has a server-side ID that must be saved."""
    def __init__(self, message: str, prompt_id: str) -> None:
        super().__init__(message)
        self.prompt_id = prompt_id


def _secret_values(payload: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if re.search(r"api.?key|secret|token|password|authorization", str(key), re.I) and isinstance(value, str) and value:
                values.add(value)
            values.update(_secret_values(value))
        extra = payload.get("extra_info")
        if isinstance(extra, dict) and re.search(r"api.?key|secret|token|password", str(extra.get("input_name", "")), re.I):
            value = extra.get("received_value")
            if isinstance(value, str) and value:
                values.add(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            values.update(_secret_values(value))
    return values


def _safe_failure(payload: Any) -> str:
    """Keep cause and node identity, never execution inputs, outputs or tracebacks."""
    fields = {"type", "message", "node_id", "node_type", "class_type", "exception_type", "exception_message"}
    def select(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): select(v) for k, v in value.items()
                    if k in fields or k in {"error", "node_errors", "errors"} or str(k).isdigit()}
        if isinstance(value, (list, tuple)):
            return [select(item) for item in value]
        return value if isinstance(value, (str, int, float, bool)) else None
    result = json.dumps(select(payload), ensure_ascii=False)
    for secret in sorted(_secret_values(payload), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result[:1600]


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ComfyError("Invalid ComfyUI endpoint port") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise ComfyError("ComfyUI endpoint must be an HTTP(S) origin without credentials, path, query or fragment")
    host = parts.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
        address = getattr(address, "ipv4_mapped", None) or address
        host = str(address)
    except ValueError:
        pass
    effective_port = port or (443 if parts.scheme == "https" else 80)
    if (host == "100.88.79.40" or host.split(".")[0] in {"thebeastiii", "beastiii"}) and effective_port == 8188:
        raise ComfyError("The production ComfyUI endpoint on node #3 port 8188 is forbidden")
    host = f"[{host}]" if ":" in host else host
    suffix = f":{effective_port}" if effective_port != (443 if parts.scheme == "https" else 80) else ""
    return f"{parts.scheme}://{host}{suffix}"


def _relative_path(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ComfyError("ComfyUI returned an invalid artifact path")
    if "\\" in value or "\x00" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise ComfyError("ComfyUI returned an unsafe artifact path")
    return value


def workflow_metadata_sha256(graph: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Hash graph dependencies without uploaded-file lists that change on every test."""
    classes = sorted({node.get("class_type") for node in graph.values() if isinstance(node, dict) and isinstance(node.get("class_type"), str)})
    relevant = {}
    for class_name in classes:
        schema = deepcopy(metadata.get(class_name))
        if isinstance(schema, dict):
            schema = {key: schema.get(key) for key in ("input", "output", "output_node")}
            filename = {"LoadImage": "image", "LoadAudio": "audio"}.get(class_name)
            if filename:
                for group in ("required", "optional"):
                    fields = (schema.get("input") or {}).get(group, {})
                    if filename in fields:
                        fields[filename] = ["UPLOADED_FILE"]
        relevant[class_name] = schema
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _workflow_input_fields(groups: dict[str, Any], inputs: dict[str, Any], prefix: str = ""):
    """Expand the flattened V3 input names exposed by ComfyUI's API exporter.

    Autogrow's container is not itself an input. Its template defines the first
    required names; DynamicCombo contributes the chosen option's nested fields.
    This mirrors comfy_api.latest._io's schema expansion without importing a
    local ComfyUI installation or evaluating node code.
    """
    for group in ("required", "optional"):
        for name, spec in groups.get(group, {}).items():
            field = f"{prefix}{name}"
            kind = spec[0] if isinstance(spec, (list, tuple)) and spec else None
            options = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            if kind == "COMFY_AUTOGROW_V3":
                template = options.get("template", {})
                names = template.get("names")
                if names is None:
                    names = [f"{template['prefix']}{i}" for i in range(template["max"])]
                template_groups = template.get("input", {})
                template_fields = [(key, value) for key in ("required", "optional") for value in template_groups.get(key, {}).values()]
                if not template_fields:
                    raise ValueError(f"Autogrow input {field!r} has no template input")
                template_group, template_spec = template_fields[0]
                for index, child in enumerate(names):
                    required = template_group == "required" and index < template.get("min", 1)
                    yield f"{field}.{child}", template_spec, required
                continue
            yield field, spec, group == "required"
            if kind == "COMFY_DYNAMICCOMBO_V3":
                for option in options.get("options", []):
                    if inputs.get(field) == option.get("key"):
                        yield from _workflow_input_fields(option.get("inputs", {}), inputs, f"{field}.")
                        break


class ComfyClient:
    def __init__(self, base_url: str | None = None, *, worker_id: str | None = None,
                 on_failure: Callable[[str], None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # The no-argument form is retained only for the explicit exclusive/local VRAM policy.
        self.base_url = validate_base_url(base_url or settings.comfy_base_url)
        self.worker_id = worker_id
        self.client_id = str(uuid.uuid4())
        self._on_failure = on_failure
        self._transport = transport
        self._workflow_secrets: set[str] = set()

    def _error(self, message: str, *, unavailable: bool = False) -> ComfyError:
        secrets = set(self._workflow_secrets)
        for name in type(settings).model_fields:
            if re.search(r"api.?key|secret|token|password", name, re.I):
                value = getattr(settings, name)
                value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
                if isinstance(value, str) and value:
                    secrets.add(value)
        for secret in sorted(secrets, key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        error = f"ComfyUI worker {self.worker_id or 'local'} ({self.base_url}): {message}"
        if unavailable and self._on_failure:
            self._on_failure(error)
        return ComfyError(error)

    async def _request(self, method: str, route: str, *, timeout: float = 30, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport, follow_redirects=False) as client:
                response = await client.request(method, f"{self.base_url}{route}", **kwargs)
        except httpx.RequestError as exc:
            raise self._error(f"{method} {route} failed: {type(exc).__name__}: {exc}", unavailable=True) from exc
        if response.status_code >= 300:
            # Never include request headers, uploaded contents, or a returned workflow graph.
            detail = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = _safe_failure(payload)
            except ValueError:
                pass
            raise self._error(f"{method} {route} returned HTTP {response.status_code}" + (f": {detail}" if detail else ""), unavailable=response.status_code >= 500)
        return response

    def _object(self, response: httpx.Response, route: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise self._error(f"{route} returned invalid JSON", unavailable=True) from exc
        if not isinstance(value, dict):
            raise self._error(f"{route} returned a non-object response", unavailable=True)
        return value

    async def health(self, *, timeout: float = 5) -> dict[str, Any]:
        return self._object(await self._request("GET", "/system_stats", timeout=timeout), "/system_stats")

    async def get_queue(self) -> dict[str, Any]:
        result = self._object(await self._request("GET", "/queue", timeout=5), "/queue")
        if any(not isinstance(result.get(k), list) for k in ("queue_running", "queue_pending")):
            raise self._error("/queue lacks queue_running or queue_pending arrays", unavailable=True)
        return result

    async def get_object_info(self) -> dict[str, Any]:
        return self._object(await self._request("GET", "/object_info"), "/object_info")

    async def validate_workflow(self, prompt: dict[str, Any], *, object_info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read-only dependency check; execution/custom validators run at /prompt submission."""
        info = await self.get_object_info() if object_info is None else object_info
        errors: list[str] = []
        for node_id, node in prompt.items():
            class_name = node.get("class_type") if isinstance(node, dict) else None
            schema = info.get(class_name)
            if not isinstance(schema, dict):
                errors.append(f"node {node_id}: class {class_name!r} is not installed")
                continue
            inputs = node.get("inputs", {})
            try:
                fields = list(_workflow_input_fields(schema.get("input") or {}, inputs))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"node {node_id} ({class_name}): invalid worker input schema ({type(exc).__name__})")
                continue
            for name, spec, required in fields:
                if required and name not in inputs:
                    errors.append(f"node {node_id} ({class_name}): required input {name!r} is missing")
                value = inputs.get(name)
                # Image/audio names are uploaded later. This check verifies model/selector enums.
                if name in {"image", "audio"} or isinstance(value, list) or value is None:
                    continue
                kind = spec[0] if isinstance(spec, (list, tuple)) and spec else None
                choices = kind if isinstance(kind, list) else None
                if kind in ("COMBO", "COMFY_DYNAMICCOMBO_V3") and len(spec) > 1:
                    choices = spec[1].get("options")
                    if kind == "COMFY_DYNAMICCOMBO_V3" and choices is not None:
                        choices = [option["key"] for option in choices]
                if choices is not None and value not in choices:
                    errors.append(f"node {node_id} ({class_name}): {name}={value!r} is not available on this worker")
        if errors:
            raise self._error("Workflow dependency validation failed: " + "; ".join(errors))
        return {"valid": True, "validation_scope": "object_info_dependencies", "worker_id": self.worker_id,
                "worker_url": self.base_url, "node_count": len(prompt)}

    async def upload_image(self, data: bytes, filename: str, *, image_type: str = "input", overwrite: bool = False) -> str:
        """The core upload/image endpoint writes arbitrary binary files, including audio."""
        _relative_path(filename)
        if image_type != "input":
            raise self._error("Uploads must target the selected worker's input directory")
        if not data:
            raise self._error(f"Cannot upload empty input {filename!r}")
        response = await self._request("POST", "/upload/image", timeout=120,
            files={"image": (filename, data, mimetypes.guess_type(filename)[0] or "application/octet-stream")},
            data={"type": image_type, "overwrite": "true" if overwrite else "false"})
        payload = self._object(response, "/upload/image")
        name = _relative_path(payload.get("name"))
        sub = _relative_path(payload.get("subfolder", ""), allow_empty=True)
        if payload.get("type") != "input":
            raise self._error("Upload response did not confirm type=input")
        return f"{sub}/{name}" if sub else name

    async def queue_prompt(self, prompt: dict[str, Any]) -> str:
        self._workflow_secrets.update(_secret_values(prompt))
        response = await self._request("POST", "/prompt", timeout=60, json={"prompt": prompt, "client_id": self.client_id})
        data = self._object(response, "/prompt")
        prompt_id = data.get("prompt_id")
        if data.get("error") or data.get("node_errors"):
            # Comfy can accept a subset of terminal branches and return node_errors alongside an ID.
            message = "Submission rejected: " + _safe_failure(data)
            if isinstance(prompt_id, str) and prompt_id:
                try:
                    await self.cancel_prompt(prompt_id)
                except asyncio.CancelledError:
                    message += f"; cleanup of accepted prompt {prompt_id} was interrupted; it may still be running"
                except ComfyError as exc:
                    message += f"; cancellation of accepted prompt {prompt_id} failed: {exc}; it may still be running"
                raise ComfySubmissionError(str(self._error(message)), prompt_id)
            raise self._error(message)
        if not isinstance(prompt_id, str) or not prompt_id:
            raise self._error("Submission response is missing prompt_id; acceptance is ambiguous; do not resubmit")
        return prompt_id

    async def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        route = f"/history/{quote(prompt_id, safe='')}"
        data = self._object(await self._request("GET", route), route)
        history = data.get(prompt_id)
        if history is not None and not isinstance(history, dict):
            raise self._error(f"History for prompt {prompt_id} is malformed")
        return history

    async def wait_for_completion(self, prompt_id: str, *, poll_interval: float | None = None,
                                  timeout: float | None = None, cancel_event: asyncio.Event | None = None) -> dict[str, Any]:
        interval = settings.poll_interval_sec if poll_interval is None else poll_interval
        limit = settings.job_timeout_sec if timeout is None else timeout
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError
            history = await self.get_history(prompt_id)
            if history is not None:
                status = history.get("status") or {}
                messages = status.get("messages") or []
                failures = [m for m in messages if isinstance(m, (list, tuple)) and m and m[0] in {"execution_error", "execution_interrupted"}]
                if history.get("node_errors") or history.get("error") or status.get("status_str") == "error" or failures:
                    raise self._error(f"Prompt {prompt_id} failed: " + _safe_failure({"errors": failures or history.get("node_errors") or history.get("error") or messages}))
                if status.get("completed") is True and status.get("status_str") == "success":
                    if not isinstance(history.get("outputs"), dict):
                        raise self._error(f"Prompt {prompt_id} completed without an outputs object")
                    return history
            await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise self._error(f"Timed out after {limit:.0f}s waiting for prompt {prompt_id}; it remains pinned and may still be running remotely")

    async def download_image(self, filename: str, *, subfolder: str = "", folder_type: str = "output") -> bytes:
        _relative_path(filename)
        _relative_path(subfolder, allow_empty=True)
        if folder_type not in {"output", "temp"}:
            raise self._error(f"Invalid output artifact type {folder_type!r}")
        response = await self._request("GET", "/view", timeout=120, params={"filename": filename, "subfolder": subfolder, "type": folder_type})
        if not response.content:
            raise self._error(f"Output artifact {filename!r} is empty")
        return response.content

    async def cancel_prompt(self, prompt_id: str) -> None:
        if not isinstance(prompt_id, str) or not prompt_id:
            raise self._error("Targeted cancellation requires a prompt_id")
        await self._request("POST", "/queue", timeout=10, json={"delete": [prompt_id]})
        await self._request("POST", "/interrupt", timeout=10, json={"prompt_id": prompt_id})

    async def interrupt(self) -> None:
        raise self._error("Instance-wide interrupt is disabled; cancel a specific prompt_id")

    async def free_memory(self, *, unload_models: bool = True, free_memory: bool = True) -> dict[str, Any]:
        before = await self.health()
        await self._request("POST", "/free", timeout=10, json={"unload_models": unload_models, "free_memory": free_memory})
        # /free schedules a flag; an immediate stats read does not prove it was executed.
        return {"requested": True, "worker_id": self.worker_id, "stats_at_request": before}
