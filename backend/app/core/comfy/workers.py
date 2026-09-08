"""One-process worker scheduling, durable pinning and explicit H3 admission."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...config import settings
from .client import ComfyClient, ComfyError, validate_base_url
from .memory import memory_snapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Worker:
    id: str
    base_url: str


def parse_workers(value: str) -> list[Worker]:
    workers: list[Worker] = []
    for item in value.split(","):
        if not item.strip():
            if value.strip():
                raise ComfyError("DS_COMFY_WORKERS contains an empty worker entry")
            continue
        worker_id, separator, url = item.strip().partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", worker_id):
            raise ComfyError("DS_COMFY_WORKERS requires unique name=http(s)://host:port entries")
        endpoint = validate_base_url(url.strip())
        if any(w.id == worker_id or w.base_url == endpoint for w in workers):
            raise ComfyError("DS_COMFY_WORKERS contains a duplicate worker ID or endpoint")
        workers.append(Worker(worker_id, endpoint))
    return workers


def _profile_eligibility(job: Any) -> set[tuple[str, str]] | None:
    if job.pipeline_id != "h3_ref2va":
        return None
    value = job.params.get("h3_eligible_workers")
    custom = job.params.get("h3_profile_id") not in {None, "builtin-official-h3"}
    if value is None and not custom:
        return None
    if not isinstance(value, list) or not value:
        if not custom and value == []:
            return None
        raise ComfyError(f"H3 profile {job.params.get('h3_profile_id')!r} has no validated worker evidence")
    pairs: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("worker_id"), str) or not isinstance(item.get("worker_url"), str):
            raise ComfyError("H3 profile snapshot contains invalid worker eligibility")
        pairs.add((item["worker_id"], validate_base_url(item["worker_url"])))
    return pairs


class WorkerRegistry:
    def __init__(self, workers: list[Worker], *, client_factory: Any = ComfyClient) -> None:
        self.workers = {worker.id: worker for worker in workers}
        self._states = {w.id: {"id": w.id, "base_url": w.base_url, "status": "unknown",
            "checked_at": None, "error": None, "queued_jobs": None, "running_jobs": None} for w in workers}
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._claims: dict[str, str] = {}
        self._next = 0

    def _down(self, worker_id: str, error: str) -> None:
        self._states[worker_id].update(status="down", checked_at=_now(), error=error, queued_jobs=None, running_jobs=None)

    def client_for(self, worker_id: str) -> ComfyClient:
        worker = self.workers.get(worker_id)
        if worker is None:
            raise ComfyError(f"Render worker {worker_id!r} is not configured in DS_COMFY_WORKERS")
        return self._client_factory(worker.base_url, worker_id=worker.id, on_failure=lambda error: self._down(worker.id, error))

    async def _refresh(self, worker_id: str) -> None:
        client = self.client_for(worker_id)
        try:
            await client.health()
            queue = await client.get_queue()
            self._states[worker_id].update(status="up", checked_at=_now(), error=None,
                queued_jobs=len(queue["queue_pending"]), running_jobs=len(queue["queue_running"]))
        except Exception as exc:
            self._down(worker_id, str(exc))

    async def status(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            await asyncio.gather(*(self._refresh(w) for w in self.workers))
        return [dict(state) for state in self._states.values()]

    async def bind_job(self, job: Any, allow_selection: bool = True) -> ComfyClient:
        from ..jobs.store import save_job
        async with self._lock:
            eligible = _profile_eligibility(job)
            if job.worker_id or job.worker_url:
                if eligible is not None and (job.worker_id, job.worker_url) not in eligible:
                    raise ComfyError(f"Job {job.id} worker pin has no matching validation/test evidence for H3 profile {job.params.get('h3_profile_id')!r}")
                worker = self.workers.get(job.worker_id)
                if not worker or worker.base_url != job.worker_url:
                    raise ComfyError(f"Job {job.id} is pinned to worker {job.worker_id!r} at {job.worker_url!r}; that exact worker configuration is unavailable. Job will not be rerouted")
            elif not allow_selection:
                raise ComfyError(f"Job {job.id} has no durable worker pin; cannot resume a submitted job")
            else:
                states = await self.status()
                if not states:
                    raise ComfyError("No render workers configured. Set DS_COMFY_WORKERS explicitly")
                requested = job.params.get("worker_id")
                requested_url = job.params.get("worker_url")
                if requested_url is not None:
                    configured = self.workers.get(requested)
                    if configured is None or configured.base_url != validate_base_url(requested_url):
                        raise ComfyError(f"Requested render worker {requested!r} changed endpoint or is no longer configured; job will not be rerouted")
                candidates = [s for s in states if s["status"] == "up"
                              and (requested is None or s["id"] == requested)
                              and (eligible is None or (s["id"], s["base_url"]) in eligible)]
                if not candidates:
                    reasons = "; ".join(f"{s['id']}: {s['error'] or s['status']}" for s in states)
                    raise ComfyError(f"No eligible render worker{f' matching {requested!r}' if requested else ''}"
                                     + (f" with validation/test evidence for H3 profile {job.params.get('h3_profile_id')!r}" if eligible is not None else "")
                                     + f". {reasons}")
                # Rotating tie-break + remote queue load + this process's in-flight claims.
                order = list(self.workers)
                rank = {key: (i - self._next) % len(order) for i, key in enumerate(order)}
                selected = min(candidates, key=lambda s: (s["queued_jobs"] + s["running_jobs"] + sum(v == s["id"] for v in self._claims.values()), rank[s["id"]]))
                self._next = (order.index(selected["id"]) + 1) % len(order)
                worker = self.workers[selected["id"]]
                job.worker_id = worker.id
                job.worker_url = worker.base_url
                job.worker_selected_at = _now()
                job.worker_selection = {"strategy": "requested" if requested else "least_queued", "workers": states}
                # This write is the boundary: no HTTP upload is allowed before it succeeds.
                save_job(job)
            self._claims[job.id] = worker.id
            return self.client_for(worker.id)

    async def release_job(self, job_id: str) -> None:
        async with self._lock:
            self._claims.pop(job_id, None)

    async def check_h3_admission(self, job: Any, client: ComfyClient) -> None:
        from ..jobs.store import save_job
        vram_threshold = settings.comfy_min_free_vram_gib
        snapshot: dict[str, Any] = {"worker_id": job.worker_id, "worker_url": job.worker_url,
            "checked_at": _now(), "ram_free_bytes": None, "ram_total_bytes": None, "devices": [],
            "metric": "vram_free", "threshold_provisional": settings.comfy_memory_threshold_provisional,
            "min_free_vram_bytes": int(vram_threshold * 1024**3) if vram_threshold is not None else None,
            "accepted": False, "error": None}
        job.memory_admission = snapshot
        problems: list[str] = []
        calibration_test = (
            job.params.get("h3_profile_test") is True
            and type(job.params.get("frames")) is int
            and job.params["frames"] == 56
        )
        if settings.comfy_memory_threshold_provisional and not calibration_test:
            problems.append(
                "Provisional H3 memory threshold permits only 56-frame profile calibration tests; "
                "production H3 is blocked until the operator sets a final DS_COMFY_MIN_FREE_VRAM_GIB "
                "and DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=false"
            )
        try:
            measurement = memory_snapshot(await client.health(), phase="admission")
            snapshot.update({key: measurement[key] for key in ("ram_free_bytes", "ram_total_bytes", "devices")})
            if snapshot["min_free_vram_bytes"] is None:
                problems.append("DS_COMFY_MIN_FREE_VRAM_GIB must be explicitly configured for H3")
            for device in snapshot["devices"]:
                free_vram, min_vram = device["vram_free_bytes"], snapshot["min_free_vram_bytes"]
                if min_vram is not None and free_vram < min_vram:
                    problems.append(f"GPU {device['index']} VRAM free={free_vram / 1024**3:.2f} GiB ({free_vram} bytes), required={min_vram / 1024**3:.2f} GiB ({min_vram} bytes)")
        except Exception as exc:
            problems.append(f"Cannot measure H3 headroom: {exc}")
        snapshot["accepted"] = not problems
        snapshot["error"] = "; ".join(problems) or None
        save_job(job)
        if problems:
            raise ComfyError(f"H3 admission failed on worker {job.worker_id} ({job.worker_url}): {snapshot['error']}")


_registry: WorkerRegistry | None = None
_registry_config: str | None = None


def get_worker_registry() -> WorkerRegistry:
    global _registry, _registry_config
    if _registry is None or _registry_config != settings.comfy_workers:
        _registry = WorkerRegistry(parse_workers(settings.comfy_workers))
        _registry_config = settings.comfy_workers
    return _registry
