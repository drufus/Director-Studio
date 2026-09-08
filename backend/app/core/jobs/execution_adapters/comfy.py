"""Execute a durable job on its explicitly pinned remote ComfyUI worker."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...comfy import ComfyError
from ...comfy.memory import H3MemorySampler
from ...schemas import JobRecord, JobStatus
from .. import store

logger = logging.getLogger("director_studio.jobs.execution.comfy")


@dataclass(frozen=True)
class ComfyExecutionRuntime:
    bind_worker: Callable[..., Awaitable[Any]]
    release_worker: Callable[[str], Awaitable[None]]
    admit_h3: Callable[[JobRecord, Any], Awaitable[None]]
    prepare: Callable[[JobRecord], Awaitable[None]]
    finish: Callable[[JobRecord], Awaitable[None]]
    update_phase: Callable[[str, str, str], Awaitable[None]]
    save_completed_outputs: Callable[..., Awaitable[JobRecord]]
    memory_sampler: Callable[..., H3MemorySampler] = H3MemorySampler


class ComfyExecutionAdapter:
    id = "comfy"
    interrupt_on_cancel = True

    @staticmethod
    def can_replay_job(job: JobRecord) -> bool:
        return (
            job.status in {JobStatus.queued, JobStatus.uploading}
            and not job.comfy_prompt_id
        )

    def can_replay(self, pipeline: Any) -> bool:
        return True

    async def run(
        self,
        job: JobRecord,
        pipeline: Any,
        inputs: dict[str, tuple[str, bytes]],
        cancel_event: asyncio.Event,
        runtime: ComfyExecutionRuntime,
    ) -> None:
        sampler = None
        try:
            if not self.can_replay_job(job):
                raise ComfyError(
                    "Refusing to submit a job that may already have reached ComfyUI; "
                    "resume its recorded prompt instead"
                )
            self._check_cancelled(cancel_event)
            # The registry persists this identity before any reference upload.
            client = await runtime.bind_worker(job, allow_selection=True)
            await runtime.prepare(job)
            self._check_cancelled(cancel_event)

            job.status = JobStatus.uploading
            store.save_job(job)
            await runtime.update_phase(job.id, job.status.value, "uploading")
            prepared_inputs = pipeline.prepare_upload_inputs(job, inputs)
            uploaded: dict[str, str] = {}
            for kind, (filename, data) in prepared_inputs.items():
                self._check_cancelled(cancel_event)
                extension = Path(filename).suffix or ".png"
                uploaded[kind] = await client.upload_image(
                    data,
                    f"ds_{job.id}_{kind}{extension}",
                )

            prompt, resolved_seed = pipeline.build_prompt(
                job,
                uploaded_images=uploaded,
            )
            job.seed = resolved_seed
            job.expected_artifacts = pipeline.expected_output_manifest(job, prompt)
            if not job.expected_artifacts:
                raise ComfyError("Pipeline did not declare its expected output manifest")
            store.save_job(job)
            self._check_cancelled(cancel_event)

            if job.pipeline_id == "h3_ref2va":
                await runtime.admit_h3(job, client)
                sampler = runtime.memory_sampler(job, client)
                await sampler.start()
                job = store.load_job(job.id) or job
            self._check_cancelled(cancel_event)
            # Persist before POST: a crash without an ID is ambiguous, never replayable.
            job.status = JobStatus.running
            store.save_job(job)
            await runtime.update_phase(job.id, job.status.value, "generating")
            prompt_id = await client.queue_prompt(prompt)
            job = store.load_job(job.id) or job
            job.comfy_prompt_id = prompt_id
            store.save_job(job)
            self._check_cancelled(cancel_event)
            await self._collect(job, pipeline, client, cancel_event, runtime, sampler)
        except asyncio.CancelledError:
            if not cancel_event.is_set():
                # Shutdown/task interruption is not a user cancellation. Preserve
                # the durable submission state for restart recovery.
                raise
            self._mark_cancelled(job.id)
        except Exception as exc:
            self._mark_failed(job.id, exc)
        finally:
            if sampler is not None:
                current = store.load_job(job.id) or job
                phase = "interrupted" if current.status == JobStatus.running else current.status.value
                try:
                    await sampler.finish(phase)
                except Exception as exc:
                    self._mark_failed(job.id, exc, stage="Memory measurement finalization failed")
            await self._finalize(job.id, runtime)

    async def resume(
        self,
        job: JobRecord,
        pipeline: Any,
        cancel_event: asyncio.Event,
        runtime: ComfyExecutionRuntime,
    ) -> None:
        sampler = None
        try:
            if not job.comfy_prompt_id:
                raise ComfyError("Cannot resume ComfyUI job without its prompt ID")
            if not job.expected_artifacts:
                raise ComfyError(
                    "Cannot resume ComfyUI job without its persisted expected output manifest"
                )
            self._check_cancelled(cancel_event)
            client = await runtime.bind_worker(job, allow_selection=False)
            await runtime.prepare(job)
            if job.pipeline_id == "h3_ref2va":
                sampler = runtime.memory_sampler(job, client)
                await sampler.start(resume=True)
            await runtime.update_phase(job.id, job.status.value, "generating")
            await self._collect(job, pipeline, client, cancel_event, runtime, sampler)
        except asyncio.CancelledError:
            if not cancel_event.is_set():
                # Shutdown/task interruption is not a user cancellation. Preserve
                # the durable submission state for restart recovery.
                raise
            self._mark_cancelled(job.id)
        except Exception as exc:
            self._mark_failed(job.id, exc)
        finally:
            if sampler is not None:
                current = store.load_job(job.id) or job
                phase = "interrupted" if current.status == JobStatus.running else current.status.value
                try:
                    await sampler.finish(phase)
                except Exception as exc:
                    self._mark_failed(job.id, exc, stage="Memory measurement finalization failed")
            await self._finalize(job.id, runtime)

    async def _collect(
        self,
        job: JobRecord,
        pipeline: Any,
        client: Any,
        cancel_event: asyncio.Event,
        runtime: ComfyExecutionRuntime,
        sampler: H3MemorySampler | None = None,
    ) -> None:
        history = await client.wait_for_completion(
            job.comfy_prompt_id,
            cancel_event=cancel_event,
        )
        self._check_cancelled(cancel_event)
        if sampler is not None:
            await sampler.finish("completed")
        await runtime.update_phase(job.id, job.status.value, "saving")
        await runtime.save_completed_outputs(
            job.id,
            pipeline=pipeline,
            client=client,
            history=history,
            cancel_event=cancel_event,
            **({"completion_check": sampler.require_complete} if sampler is not None else {}),
        )

    async def cancel(self, job: JobRecord, runtime: ComfyExecutionRuntime) -> None:
        if not job.comfy_prompt_id:
            return
        client = await runtime.bind_worker(job, allow_selection=False)
        await client.cancel_prompt(job.comfy_prompt_id)

    @staticmethod
    def _check_cancelled(cancel_event: asyncio.Event) -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError

    @staticmethod
    def _mark_cancelled(job_id: str) -> None:
        job = store.load_job(job_id)
        if job is None:
            return
        # A failed prompt-specific cancellation must remain visibly failed.
        if job.status == JobStatus.failed or (job.status == JobStatus.cancelled and job.error):
            return
        job.status = JobStatus.cancelled
        job.error = "Cancelled by user"
        store.save_job(job)

    @staticmethod
    def _mark_failed(job_id: str, exc: Exception, *, stage: str | None = None) -> None:
        logger.error("ComfyUI job %s failed%s: %s", job_id, f" during {stage}" if stage else "", exc)
        job = store.load_job(job_id)
        if job is None:
            return
        submitted_id = getattr(exc, "prompt_id", None)
        if isinstance(submitted_id, str) and submitted_id.strip():
            # Comfy may accept a partial graph while reporting node_errors.
            # Preserve that identity even if its prompt-specific cancellation failed.
            job.comfy_prompt_id = submitted_id
        detail = f"{stage}: {exc}" if stage else str(exc)
        if stage and job.error:
            detail = f"{job.error}; {detail}"
        job.status = JobStatus.failed
        job.error = detail[:2000]
        store.save_job(job)

    async def _finalize(
        self,
        job_id: str,
        runtime: ComfyExecutionRuntime,
    ) -> None:
        job = store.load_job(job_id)
        if job is None:
            await runtime.release_worker(job_id)
            return
        try:
            if job.status in {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}:
                await runtime.finish(job)
        except Exception as exc:
            self._mark_failed(job.id, exc, stage="GPU policy finalization failed")
        try:
            await runtime.release_worker(job_id)
        except Exception as exc:
            self._mark_failed(job.id, exc, stage="Worker reservation release failed")
        try:
            from ..shot_sync import on_pipeline_job_terminal

            on_pipeline_job_terminal(store.load_job(job_id) or job)
        except Exception as exc:
            self._mark_failed(job.id, exc, stage="Terminal shot synchronization failed")
            # Reflect the failed result in a shot that may have been partly updated.
            try:
                on_pipeline_job_terminal(store.load_job(job_id) or job)
            except Exception as sync_exc:
                logger.error("Could not sync failed job %s: %s", job_id, sync_exc)
