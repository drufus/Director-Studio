# Remote ComfyUI workers — Phase 3

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

Set both `DS_COMFY_MIN_FREE_RAM_GIB` and `DS_COMFY_MIN_FREE_VRAM_GIB` to positive, workflow-specific thresholds before any H3 test or production submission. There is no assumed safe threshold. An unset value fails admission and records whatever actual measurements were available. The settings apply to each configured worker; each reported GPU must meet the VRAM threshold. On a unified-memory Spark, RAM and VRAM are overlapping capacity measurements, so they are checked independently and never added together.

Only node #8 is initially configured. The down H3 instance on node #3 port 8189 stays unconfigured. Node #1 requires the operator's go-ahead and its prior metadata probe did not advertise the H3 Ref2AV class. Node #3 port 8188 is forbidden, including for health checks; origin validation rejects its known host/IP spellings and HTTP redirects are not followed. See [accepted topology](CLUSTER-PORT-PLAN.md).

Adding workers means adding explicit comma-separated `name=http://host:port` entries. IDs and endpoints must be unique. The scheduler uses queue counts and in-process reservations, with a rotating tie-break. It records every candidate's health in the job selection snapshot. Unreachable configured workers remain visible as down. An empty registry is an explicit configuration error for rendering.

## Durable job and file contract

1. Save `worker_id`, immutable `worker_url`, `worker_selected_at`, and `worker_selection` atomically before uploading anything.
2. Validate declared input references. Upload each image/audio file to that worker's `/upload/image` endpoint and patch graph inputs with returned names. The actor pipeline uploads its generated blank placeholder too.
3. Build the graph and persist `expected_artifacts`, identifying designated output nodes and logical outputs before submission.
4. For H3, fetch `/system_stats` immediately before submission and save `memory_admission`: timestamp, worker, measured free/total RAM and VRAM, configured thresholds, decision, and error. Missing measurements, request failures, and insufficient headroom stop submission with specific numbers/cause. No models are evicted and no alternate worker is tried.
5. Mark the job running durably before POST `/prompt`. Save its accepted ID. A partial acceptance containing `node_errors` fails, retains its ID, and attempts targeted cancellation; a cancellation failure remains visible.
6. Poll `/history/{id}` on the pinned worker. Require explicit successful completion and the manifest's complete designated output set. Retrieve each output through `/view` into the app's `data/`. Empty/missing files, node errors, mapper mismatch, postprocessing errors, and success-hook errors cannot produce success.

Cancellation deletes only the selected prompt ID from `/queue` and sends that ID to `/interrupt`. A timeout reports that the remote prompt may still be running; it never resubmits or switches workers. Restart recovery resumes submitted IDs on the same endpoint. Only queued/uploading jobs without an ID replay; running jobs without an ID fail as ambiguous. An old submitted job without worker identity or an artifact manifest also fails explicitly; this deployment uses a fresh data root.

The [ComfyUI v0.30.2 server source](https://github.com/Comfy-Org/ComfyUI/blob/v0.30.2/server.py) implements binary upload, output retrieval, queue deletion, and prompt-targeted interruption. This transport uses those endpoints directly. It does not use a CLI export folder.

## UI and health

`GET /api/comfy/workers` returns each configured worker's ID, endpoint, status, last check, queue/running counts, and specific error. The app header refreshes fleet status every ten seconds; Settings shows the complete worker table. Job views show worker identity and memory admission evidence, including mobile errors. `/api/health` includes fleet readiness and the selected LLM model; `/api/health/live` remains local process liveness.

Independent GPU policy keeps Director chat available during multiple queued/uploading/running renders and performs no automatic model release. An explicit manual release API action requires a worker ID in independent mode. Genuine active-chat lifecycle protection remains in force.

Worker errors retain safe node identity and failure messages while excluding execution input/output dumps, tracebacks, and validation received-values. Configured runtime credentials and recognized credential fields are redacted. Do not put credentials inside imported workflow JSON; use the private runtime environment for service credentials.

## H3 phase boundary

The setup UI now explicitly selects a worker for metadata inspection, dependency validation, and the 56-frame test job. Unreachable metadata fails visibly. Dependency validation uses read-only `/object_info` to check installed classes, required inputs, and enum selections; it does not submit a render or claim that custom node execution validators have run. `/prompt` performs execution validation at submission.

Phase 4 still must bind profile inspection/validation/test/activation evidence to the same worker, reject broken explicit profile selections instead of the existing bundled fallback, and validate the real H3 graph end to end. The current pipeline `workflow.py` contracts and shipped workflow JSON are unchanged. This Phase 3 PR is not production H3 acceptance.

## Verification and remaining acceptance

Backend tests cover N-worker concurrent execution, durable pins before upload, immutable resume endpoints, worker failure without reroute, H3 memory rejection, partial accepted graphs, incomplete downloads, failed hooks, uncertain restart states, targeted cancellation, metadata failures, and redacted error diagnostics. Frontend tests cover worker outages, multiple active jobs, explicit setup selection, memory numbers, and mobile failures. The existing 16 Windows-only packaging cases remain excluded; no new quarantine was added.

Local Phase 3 verification: 1,103 backend tests and 210 frontend tests pass; frontend production build passes. Native CI repeats backend/frontend/build/startup checks on macOS, Linux x64, and Linux ARM64.

The read-only probe on 2026-09-08 at 04:19 UTC through the new registry reported node #8 up, with no running/pending queue entries, H3 Ref2AV class present, 11,689,590,784 bytes free system RAM (10.89 GiB), and 5,147,022,280 bytes free VRAM (4.79 GiB). These are point-in-time measurements, not guaranteed admission headroom. Sanitized evidence remains at `.tmp/phase3-readonly-worker-probe.json`.

Live work in this phase is read-only against authorized node #8. No actor or H3 render has been submitted, and no service has been deployed. A real actor asset on each of two workers remains deferred until the operator enables a second suitable worker. Phase 4 owns the real H3 import/test/production checks; Phase 5 owns node #6 systemd deployment, Tailscale browser acceptance, private env installation, and durable logs. Polling remains the v1 progress mechanism; websocket progress, stronger cancellation race handling, and whole-set artifact promotion remain deferred.
