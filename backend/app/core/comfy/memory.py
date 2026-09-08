"""Durable sampled GPU-pool telemetry for one pinned H3 execution.

ComfyUI's RAM and VRAM figures can describe the same unified memory pool.
Only VRAM gates admission; each device is recorded separately and never summed.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

from ...config import settings
from .client import ComfyError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value)


def memory_snapshot(stats: dict[str, Any], *, phase: str, sampled_at: str | None = None) -> dict[str, Any]:
    """Reject invalid GPU telemetry while keeping RAM purely informational."""
    system = stats.get("system")
    system = system if isinstance(system, dict) else {}
    snapshot: dict[str, Any] = {
        "sampled_at": sampled_at or now(), "phase": phase,
        "ram_free_bytes": _bytes(system.get("ram_free")),
        "ram_total_bytes": _bytes(system.get("ram_total")), "devices": [],
    }
    devices = stats.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ComfyError("/system_stats has no GPU devices")
    indices: set[int] = set()
    for device in devices:
        if not isinstance(device, dict):
            raise ComfyError("/system_stats contains an invalid GPU device")
        index = device.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index in indices:
            raise ComfyError("/system_stats has a missing or duplicate GPU device index")
        indices.add(index)
        free, total = _bytes(device.get("vram_free")), _bytes(device.get("vram_total"))
        if free is None or total is None or total == 0 or free > total:
            raise ComfyError(f"/system_stats GPU {index} has invalid vram_free/vram_total")
        snapshot["devices"].append({"index": index, "name": device.get("name"),
                                    "vram_free_bytes": free, "vram_total_bytes": total})
    return snapshot


class H3MemorySampler:
    """Sample independently of prompt polling; measurement failures never lose the prompt.

    Every mutation reloads the latest job synchronously before its atomic write.
    A failed sample is durable immediately. Polling continues until the prompt is
    terminal, then incomplete measurements fail the job before profile proof hooks.
    """

    def __init__(self, job: Any, client: Any, *, interval: float | None = None) -> None:
        self.job_id = job.id
        self.worker_id, self.worker_url = job.worker_id, job.worker_url
        self.client = client
        self.interval = interval if interval is not None else settings.comfy_memory_sample_interval_sec
        self.request_timeout = settings.comfy_memory_request_timeout_sec
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._finished = False

    def _job(self) -> Any:
        from ..jobs import store
        job = store.load_job(self.job_id)
        if job is None:
            raise ComfyError(f"Memory measurement job {self.job_id} disappeared")
        if (job.worker_id, job.worker_url) != (self.worker_id, self.worker_url):
            raise ComfyError(f"Memory measurement worker pin changed for job {self.job_id}")
        return job

    def _save(self, job: Any) -> None:
        from ..jobs import store
        store.save_job(job)

    def _record_error(self, phase: str, exc: Exception, **timing: Any) -> None:
        job = self._job()
        usage = job.memory_usage
        usage["errors"].append({"sampled_at": now(), "phase": phase, **timing,
                                "error": f"Worker {self.worker_id} ({self.worker_url}): {exc}"})
        usage["status"] = "incomplete"
        self._save(job)

    async def start(self, *, resume: bool = False) -> None:
        job = self._job()
        if job.memory_usage is None or not resume:
            job.memory_usage = {
                "status": "recording", "worker_id": self.worker_id, "worker_url": self.worker_url,
                "sample_interval_sec": self.interval, "request_timeout_sec": self.request_timeout,
                "started_at": now(), "finished_at": None,
                "sample_count": 0, "devices": [], "samples": [], "errors": [],
                "note": "Observed samples of each worker GPU memory pool, not exact allocator peaks or isolated job allocations. RAM is informational; overlapping pools are never summed.",
            }
            self._save(job)
        elif (job.memory_usage.get("worker_id"), job.memory_usage.get("worker_url")) != (self.worker_id, self.worker_url):
            raise ComfyError("Persisted memory measurements belong to a different worker")
        if resume:
            usage = job.memory_usage
            samples = usage.get("samples")
            if (
                usage.get("status") == "completed" and usage.get("errors") == []
                and isinstance(usage.get("finished_at"), str) and usage["finished_at"]
                and isinstance(samples, list) and len(samples) >= 2
                and type(usage.get("sample_count")) is int and usage["sample_count"] == len(samples)
                and isinstance(samples[-1], dict) and samples[-1].get("phase") == "completed"
                and usage.get("devices")
            ):
                # The render was fully observed before output download began.
                # Recover its artifacts without inventing a gap after execution.
                self._finished = True
                return
            # Preserve the original baseline and peak. A restart leaves an
            # unobserved interval, so this run cannot certify a complete peak.
            self._record_error("resume", ComfyError("Memory sampling resumed after an unobserved interval; prior samples and peak are preserved"))
        await self.sample("resume" if resume else "submit", strict=not resume)
        self._task = asyncio.create_task(self._run(), name=f"h3-memory-{self.job_id}")

    async def sample(self, phase: str, *, strict: bool = False) -> None:
        requested_at, started = now(), time.monotonic()
        try:
            snapshot = memory_snapshot(await self.client.health(timeout=self.request_timeout), phase=phase)
            snapshot.update(requested_at=requested_at, request_duration_sec=time.monotonic() - started)
            job = self._job()
            usage = job.memory_usage
            current = {d["index"]: d for d in usage["devices"]}
            observed = {d["index"]: d for d in snapshot["devices"]}
            if current and (set(current) != set(observed) or any(
                d["vram_total_bytes"] != observed[index]["vram_total_bytes"] for index, d in current.items()
            )):
                raise ComfyError("/system_stats GPU device identity or total memory changed during sampling")
            for device in snapshot["devices"]:
                free, total = device["vram_free_bytes"], device["vram_total_bytes"]
                summary = current.get(device["index"])
                if summary is None:
                    summary = {"index": device["index"], "name": device["name"], "vram_total_bytes": total,
                               "baseline_vram_free_bytes": free, "min_vram_free_bytes": free,
                               "peak_vram_used_bytes": total - free, "peak_vram_delta_bytes": 0,
                               "peak_at": snapshot["sampled_at"]}
                    usage["devices"].append(summary)
                elif free < summary["min_vram_free_bytes"]:
                    summary.update(min_vram_free_bytes=free, peak_vram_used_bytes=total - free,
                                   peak_vram_delta_bytes=max(0, summary["baseline_vram_free_bytes"] - free),
                                   peak_at=snapshot["sampled_at"])
            usage["samples"].append(snapshot)
            usage["sample_count"] += 1
            self._save(job)
        except Exception as exc:
            self._record_error(phase, exc, requested_at=requested_at, request_duration_sec=time.monotonic() - started)
            if strict:
                raise ComfyError(f"H3 submit memory measurement failed: {exc}") from exc

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                await self.sample("execution")

    async def finish(self, phase: str) -> None:
        if self._finished:
            return
        self._stop.set()
        if self._task is not None:
            # No new interval starts; an in-flight request has the client's
            # bounded /system_stats timeout, and its result is retained.
            await asyncio.shield(self._task)
        await self.sample(phase)
        job = self._job()
        job.memory_usage["finished_at"] = now()
        job.memory_usage["status"] = (
            "interrupted" if phase == "interrupted" else
            "incomplete" if job.memory_usage["errors"] else "completed"
        )
        self._save(job)
        self._finished = True

    def require_complete(self) -> None:
        usage = self._job().memory_usage
        if usage["errors"]:
            raise ComfyError(
                f"H3 render completed on worker {self.worker_id}, but memory measurements are incomplete: "
                + "; ".join(error["error"] for error in usage["errors"][-3:])
            )
