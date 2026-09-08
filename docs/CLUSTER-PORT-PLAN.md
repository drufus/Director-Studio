# Accepted cluster port decisions

Phase 1 supplies native macOS/Linux build, startup, and tests. Subsequent phases remain separate PRs; this file records the accepted constraints and deferred work so native startup is not mistaken for completed cluster inference.

## Runtime and transport

- One Director process on node #6. Worker concurrency belongs inside that process; replicated API services and durable distributed task claiming are out of scope.
- Use direct HTTP for the cluster's ComfyUI transport. Remove comfy-mcp/comfy-cli from that path when the transport phase lands; no dual transport is required for a hypothetical single-box deployment.
- Design the registry for N render workers. The initial configured fleet contains only `beastviii=http://100.93.117.98:8188`. Additional workers require explicit operator configuration; a configured worker that becomes unavailable remains visible as down.
- Persist worker ownership before upload. Submission, history, output downloads, evidence, and cancellation stay on that worker; no silent substitution of another worker, backend, model, or workflow.

## Accepted initial worker topology

The operator's latest topology ruling supersedes the earlier instruction to configure both render nodes immediately.

| Node / instance | Initial status and permitted use |
| --- | --- |
| beastviii, `http://100.93.117.98:8188` | The only initially configured render worker. |
| beastiii H3, `http://100.88.79.40:8189` | Down. Keep it out of the initial configuration until the operator enables it. |
| beastiii production, `100.88.79.40:8188` | Forbidden. Never contact it, including for health checks. |
| Node #1, `http://100.88.15.58:8188` | Candidate only; do not configure it without the operator's go-ahead. The authorized `/object_info` probe on 2026-09-08 at 03:40 UTC returned HTTP 200 but **did not contain `MiniMaxH3ReferenceToVideo`**. It does not currently advertise the class required by the H3 workflow. |

The node #1 probe contacted only `/object_info`; it did not inspect memory, change configuration, or submit a job. Sanitized probe evidence is stored locally at `.tmp/node1-h3-object-info-probe.json`.

The operator reports high memory use: node #8 at 110/121 GiB RAM, 3.7 GiB swap, and nine containers; node #1 at 103/121 GiB RAM. These figures are operator reports, not measurements from the metadata probe. No host memory measurements or render submissions were performed as part of this metadata check.

The two-worker acceptance requirement is **deferred, not dropped**. Preserve the tests and design for two separately pinned jobs and visible worker failures. A successful initial deployment with one worker does not complete that acceptance requirement; it remains pending until the operator enables a second suitable worker.

## Required memory admission for long H3 jobs

Phase 3 must add configurable minimum headroom for both free system memory and free render-device VRAM. Immediately before a long H3 submission, query the pinned worker's `/system_stats` and persist the measured free/total system memory, free/total device VRAM, timestamp, worker identity, and evaluated thresholds in the job record. Store numeric measurements in bytes and show readable units in errors.

If the snapshot cannot be fetched, required measurements are missing, or either threshold is unmet, fail submission loudly. The job error must identify the worker and specific cause; a low-headroom error must include the measured available amount and required threshold. Do not submit, silently reroute, or automatically evict another service's models to make the check pass. Threshold values must be configured for the working H3 workflow before production-length renders are enabled.

## LLM and model selection

Before adapter implementation, authenticate to the existing LiteLLM router using its host-side env file. Never print, log, paste, or commit the key. Probe each candidate for actual image support, JSON-schema constraint behavior, and stable native tool-call IDs. A model name or catalog entry is not a capability guarantee. Report the results before finalizing the adapter design.

No first-entry model auto-selection/persistence. Report blank selection as blank; keep the picker usable while chat is disabled. Provider-scope persisted selections so a legacy Ollama tag cannot override the new provider configuration. Keep the current picker and model-selection persistence concept while deliberately updating their tests.

Preserve deterministic parsing and the existing bounded, same-model JSON repair paths for planning, storyboard verdicts, six-section prompts, and visual briefs. Document each preserved repair in its implementation PR. Remove image-to-text retries, tool-protocol switching after XML failures, and nonstream regeneration after partial streaming; surface those failures instead.

The current operator ruling confirms `kasari-flash` as the Qwen3.8-Flash-Next NVFP4 route. Report the exact catalog ID and selection source; do not treat the name as suspect or alias it to another name. Record reasoning versus payload token usage for the explicit 8192-token planning budget, marking unavailable server breakdowns as unknown rather than estimating.

Vision is a routing requirement. The operator identifies `kasari-vision` (Qwen3-VL-32B NVFP4, node #2) and `kasari-vision-deep` (MiniCPM-V 4.5, node #8). Check which exact IDs LiteLLM advertises and probe image content parts plus JSON-schema response format for advertised routes. Do not design a disabled-vision deployment fallback unless neither is routable. The latest authenticated roll call advertised neither dedicated vision ID; the separately tested `kasari-flash` image path remains available with explicit configuration. Never silently omit images, substitute an absent route, or skip analysis.

## Chat must remain usable during remote renders

Independent-worker mode must disable the shared-GPU policy at every layer:

1. API admission, including `_assert_chat_available` on normal and streaming chat routes.
2. Session admission, including reservation checks inside `llm_session`.
3. Frontend admission, including the server's `chat_locked` field and chat-control eligibility.
4. LLM warm/unload and automatic Comfy `/free` operations associated with residency handoff.

Acceptance tests must hold a remote render reservation in queued, uploading, and running states and verify that a Director conversation on the separate LLM node is admitted and completes. The same tests must prove that chat makes no Ollama warm/unload calls and triggers no Comfy model eviction. Keep genuine per-project active-chat lifecycle protection. If the shared-GPU mode is retained, test its intentional blocking separately; merely changing a status label or storing a policy string is insufficient.

## H3 profile contract

Preserve the current `validator.py`/`inspector.py` runtime-import contract: an explicitly selected terminal output, an upstream selected `MiniMaxH3ReferenceToVideo`, optional seed mapping, no `ref_frame`/`last_frame` on the selected boundary, `VHS_VideoCombine` terminals, and unrelated branches. Do not add global exactly-one-node counts. The multistage fixture and tests remain valid.

An explicitly selected profile that is missing, changed, or invalid must stop submission and name the requested identity. An explicit built-in selection remains supported. Missing initial configuration and failure of an existing selection are distinct outcomes with different messages. Inspection, validation, test render, and activation evidence must refer to the selected worker, including the HTTP success hook needed to record H3 test evidence.

## Priority and release boundary

P0: native setup; the complete LLM adapter; effective residency/admission policy; N-worker registry and HTTP transport with only beastviii initially configured; durable pinning before upload; worker-bound H3 inspection/validation/test/evidence; memory-headroom preflight and recorded measurements before long H3 submissions; systemd service on node #6 with Tailscale access, private runtime env file, and durable logs.

P1 must ship before real production renders: only queued/uploading jobs are replayable; declare expected artifacts before submission and verify completeness; never treat `node_errors` plus a partial nonempty map as success; expose postprocessing and success-hook failures in job state. A 60–90 minute render must not be silently resubmitted after an uncertain submission.

P2 is deferred: `/ws` progress (polling is acceptable for v1), cancellation race hardening beyond prompt-specific interrupt, staging/promoting a whole artifact set after partial downloads, and legacy Windows absolute-path migration. The node uses a fresh data root.

The deployment PR will follow existing `kasari-*.service` patterns, use `/opt/kasari/config/` with mode-600 env files and `Restart=on-failure`, and document install/health/log locations in `docs/DEPLOY-CLUSTER.md`. Startup must not erase prior logs, even if a later packaging layer recreates its runtime directory.
