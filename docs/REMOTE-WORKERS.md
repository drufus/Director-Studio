# Remote ComfyUI workers

Director Studio runs one API process and schedules Comfy jobs across an N-worker registry. It needs no local ComfyUI installation, comfy-mcp, comfy-cli, or shared disk. macOS development and the Linux service use the same transport and data model.

## Runtime configuration

Copy `backend/.env.example` through the native setup script, then edit the private runtime env file. Preserve mode 600. Keys stay in that file and are loaded at runtime; never paste them into commands, documentation, or job data.

```dotenv
DS_COMFY_WORKERS=beastviii=http://100.93.117.98:8188
DS_LLM_PROVIDER=openai_compatible
DS_LLM_BASE_URL=http://100.124.186.11:4000/v1
DS_LLM_API_KEY=[REDACTED]
DS_LLM_MODEL=kasari-flash
DS_LLM_VISION_MODELS=kasari-flash
DS_DIRECTOR_NUM_PREDICT=8192
DS_VRAM_POLICY=independent
DS_H3_PROVIDER=local
DS_JOB_TIMEOUT_SEC=7200
```

`[REDACTED]` is a documentation marker, not a working credential. Model and vision settings reflect the verified Phase 2 route; model discovery still checks the exact live roster and reports a removed model. This phase does not change LLM selection or capability behavior.

Set `DS_COMFY_MIN_FREE_VRAM_GIB` to the operator's workflow-specific admission threshold. Each reported render device must meet it; missing VRAM measurements or an unset threshold fail admission. `ram_free` is recorded for context and does not gate rendering. The GB10 reports RAM and VRAM for an overlapping unified pool, with substantially lower `vram_free`; admission uses that lower measurement without averaging the two. `DS_COMFY_MIN_FREE_RAM_GIB` is no longer used.

For the authorized 56-frame calibration only, use `DS_COMFY_MIN_FREE_VRAM_GIB=1` with `DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=true`. This low provisional threshold permits measurement; it is not a production recommendation. `DS_COMFY_MEMORY_SAMPLE_INTERVAL_SEC=1` sets the minimum delay after each periodic `/system_stats` request completes, rather than a fixed sampling frequency. Actual observation timestamps include the effect of request latency. `DS_COMFY_MEMORY_REQUEST_TIMEOUT_SEC=30` controls the separate statistics timeout; any timeout remains visible and makes test calibration incomplete. The operator will choose the production threshold from the measured peak plus margin.

Only node #8 is configured, and no second worker is planned. The H3 instance on node #3 port 8189 was deliberately retired. Node #1 remains unconfigured; its prior metadata probe did not advertise the H3 Ref2AV class. Node #3 port 8188 is forbidden, including for health checks; origin validation rejects its known host/IP spellings and HTTP redirects are not followed. The registry and tests retain support for N workers. See [accepted topology](CLUSTER-PORT-PLAN.md).

Adding workers means adding explicit comma-separated `name=http://host:port` entries. IDs and endpoints must be unique. The scheduler uses queue counts and in-process reservations, with a rotating tie-break. It records every candidate's health in the job selection snapshot. Unreachable configured workers remain visible as down. An empty registry is an explicit configuration error for rendering.

## Durable job and file contract

1. Save `worker_id`, immutable `worker_url`, `worker_selected_at`, and `worker_selection` atomically before uploading anything.
2. Validate declared input references. Upload each image/audio file to that worker's `/upload/image` endpoint and patch graph inputs with returned names. The actor pipeline uploads its generated blank placeholder too.
3. Build the graph and persist `expected_artifacts`, identifying designated output nodes and logical outputs before submission.
4. For H3, fetch `/system_stats` immediately before submission and save `memory_admission`: timestamp, worker, measured free/total RAM and VRAM, the VRAM threshold, provisional status, decision, and error. Missing VRAM measurements, request failures, and insufficient VRAM stop submission with specific numbers/cause. RAM is informational. No models are evicted and no alternate worker is tried.
5. Mark the job running durably before POST `/prompt`. Save its accepted ID. A partial acceptance containing `node_errors` fails, retains its ID, and attempts targeted cancellation; a cancellation failure remains visible.
6. Poll `/history/{id}` on the pinned worker. Require explicit successful completion and the manifest's complete designated output set. Retrieve each output through `/view` into the app's `data/`. Empty/missing files, node errors, mapper mismatch, postprocessing errors, and success-hook errors cannot produce success.

H3 jobs also persist `memory_usage` with submission, periodic, and completion samples; baseline free VRAM; minimum observed free VRAM; sampled peak used VRAM and its timestamp; change from baseline; and telemetry errors. These are per-device measurements of the whole worker pool, including other services. They are not an exclusive allocation for the job, and an interval sample can miss a shorter spike. A telemetry gap is visible and prevents a complete measurement claim.

Cancellation deletes only the selected prompt ID from `/queue` and sends that ID to `/interrupt`. A timeout reports that the remote prompt may still be running; it never resubmits or switches workers. Restart recovery resumes submitted IDs on the same endpoint. Only queued/uploading jobs without an ID replay; running jobs without an ID fail as ambiguous. An old submitted job without worker identity or an artifact manifest also fails explicitly; this deployment uses a fresh data root.

The [ComfyUI v0.30.2 server source](https://github.com/Comfy-Org/ComfyUI/blob/v0.30.2/server.py) implements binary upload, output retrieval, queue deletion, and prompt-targeted interruption. This transport uses those endpoints directly. It does not use a CLI export folder.

## UI and health

`GET /api/comfy/workers` returns each configured worker's ID, endpoint, status, last check, queue/running counts, and specific error. The app header refreshes fleet status every ten seconds; Settings shows the complete worker table. Job views show worker identity, VRAM admission evidence, provisional status, and sampled memory peaks, including errors. `/api/health` includes fleet readiness and the selected LLM model; `/api/health/live` remains local process liveness.

Independent GPU policy keeps Director chat available during multiple queued/uploading/running renders and performs no automatic model release. An explicit manual release API action requires a worker ID in independent mode. Genuine active-chat lifecycle protection remains in force.

Worker errors retain safe node identity and failure messages while excluding execution input/output dumps, tracebacks, and validation received-values. Configured runtime credentials and recognized credential fields are redacted. Do not put credentials inside imported workflow JSON; use the private runtime environment for service credentials.

## H3 workflow evidence

The setup UI explicitly binds each import to a worker ID and URL for metadata inspection, dependency validation, the 56-frame test, and activation. Changing or clearing the worker invalidates its evidence; changed relevant metadata or graph boundaries require revalidation and testing. Each custom profile records separate eligible-worker proofs, and scheduling uses only matching configured endpoints. An explicitly selected missing, changed, or invalid profile blocks submission and names the requested identity. Selecting the built-in is an explicit recovery action; it is never substituted automatically.

Dependency validation uses read-only `/object_info`, including Comfy V3 flattened Autogrow and nested DynamicCombo inputs. It does not queue a render or execute custom node validators. `/prompt` performs execution validation, and successful artifact retrieval supplies test evidence. The `workflow.py` contracts and shipped workflow JSON remain unchanged.

See [H3 worker profiles](H3-WORKER-PROFILES.md) for the lifecycle, SPARK export, live acceptance record, and instrumented calibration. The original unmeasured threshold has been withdrawn. Node #8 was rebooted to clear orphaned unified-pool residue; the operator reports about 53 GiB free RAM and 32.6 GiB free VRAM after boot and model load. Each submission still measures current VRAM rather than relying on this report.

## Verification and remaining acceptance

Backend tests cover N-worker concurrent execution, durable pins before upload, immutable resume endpoints, worker failure without reroute, H3 memory rejection, partial accepted graphs, incomplete downloads, failed hooks, uncertain restart states, targeted cancellation, metadata failures, and redacted error diagnostics. Frontend tests cover worker outages, multiple active jobs, explicit setup selection, memory numbers, and mobile failures. The existing 16 Windows-only packaging cases remain excluded; no new quarantine was added.

Current local verification: 1,227 backend tests and 236 frontend tests pass; frontend production build passes. Native CI repeats backend/frontend/build/startup checks on macOS, Linux x64, and Linux ARM64.

The read-only probe on 2026-09-08 at 04:19 UTC through the new registry reported node #8 up, with no running/pending queue entries, H3 Ref2AV class present, 11,689,590,784 bytes free system RAM (10.89 GiB), and 5,147,022,280 bytes free VRAM (4.79 GiB). These are point-in-time measurements, not guaranteed admission headroom. Sanitized evidence remains at `.tmp/phase3-readonly-worker-probe.json`.

The post-reboot 56-frame test `job_cd3e1511d6f6` completed and downloaded its video with 225 samples and no telemetry errors. Its warm-run pool peak was **119.9887 GiB**, an increase of **2.7458 GiB** from the submission baseline. The preceding cold run `job_126264783711` observed **120.5918 GiB** peak pool usage and a **31.5339 GiB** increase, but one statistics timeout made its calibration incomplete and the app correctly failed the job even though ComfyUI rendered it. These are whole-pool samples, not exclusive job allocations; the cold-run gap may have hidden a higher peak. The [Phase 4 acceptance record](H3-WORKER-PROFILES.md#post-reboot-calibration-results) includes exact bytes, timestamps, and the cold/warm distinction. The operator has not yet set the production threshold.

The operator's current topology supersedes the earlier two-worker live acceptance requirement: there is one worker and no second planned. Settings successfully activated the SPARK custom profile after the video loaded without an error. Production admission still awaits the operator's threshold; Phase 5 owns node #6 systemd deployment, Tailscale browser acceptance, private env installation, and durable logs. Polling remains the v1 progress mechanism; websocket progress, stronger cancellation race handling, and whole-set artifact promotion remain deferred.
