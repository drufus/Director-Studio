# Accepted cluster port decisions

Phase 1 supplies native macOS/Linux build, startup, and tests. Subsequent phases remain separate PRs; this file records the accepted constraints and deferred work so native startup is not mistaken for completed cluster inference.

## Runtime and transport

- One Director process on node #6. Worker concurrency belongs inside that process; replicated API services and durable distributed task claiming are out of scope.
- Use direct HTTP for the cluster's ComfyUI transport. Remove comfy-mcp/comfy-cli from that path when the transport phase lands; no dual transport is required for a hypothetical single-box deployment.
- Design the registry for N render workers. Configure only `beastviii=http://100.93.117.98:8188`; no second worker is planned. Additional workers would require explicit operator configuration; a configured worker that becomes unavailable remains visible as down.
- Persist worker ownership before upload. Submission, history, output downloads, evidence, and cancellation stay on that worker; no silent substitution of another worker, backend, model, or workflow.

## Accepted worker topology

The operator's 2026-09-08 topology ruling supersedes both the original two-worker plan and the later deferred two-worker acceptance requirement.

| Node / instance | Status and permitted use |
| --- | --- |
| beastviii, `http://100.93.117.98:8188` | The only configured render worker. ComfyUI 0.30.2 is back after a reboot. |
| beastiii H3, `http://100.88.79.40:8189` | Deliberately retired. This install was on node #3, not node #8; do not configure it. |
| beastiii production, `100.88.79.40:8188` | Forbidden. Never contact it, including for health checks. |
| Node #1, `http://100.88.15.58:8188` | Candidate only; do not configure it without the operator's go-ahead. The authorized `/object_info` probe on 2026-09-08 at 03:40 UTC returned HTTP 200 but **did not contain `MiniMaxH3ReferenceToVideo`**. It does not currently advertise the class required by the H3 workflow. |

The node #1 probe contacted only `/object_info`; it did not inspect memory, change configuration, or submit a job. Sanitized probe evidence is stored locally at `.tmp/node1-h3-object-info-probe.json`.

The operator reports that node #8's earlier high usage was orphaned unified-pool residue, cleared by reboot. Reported steady state after boot and model load is about 68 GiB used, 53 GiB available, and zero swap. `/system_stats` reports roughly 53 GiB `ram_free` and 32.6 GiB `vram_free` for the same unified pool. These are operator-reported readings; each job records its own fresh measurement.

Acceptance targets the one configured worker. Preserve the N-worker registry, tests for separately pinned jobs, and visible worker failures; there is no special single-worker transport or degraded execution path.

## Required memory admission for long H3 jobs

Evaluate H3 admission against each render device's `/system_stats` `vram_free` only. RAM is informational; do not gate on it or average RAM and VRAM. The GB10 reports these overlapping pool measurements differently, and `vram_free` is the lower render constraint. Immediately before submission, persist measured free/total RAM and VRAM, timestamp, worker identity, the VRAM threshold, and whether that threshold is provisional. Store numeric measurements in bytes and show readable units in errors.

If the snapshot cannot be fetched, required VRAM measurements are missing, or the VRAM threshold is unmet, fail submission loudly with the worker, cause, available amount, and required threshold. Do not silently reroute or evict another service's models. The previous unmeasured threshold is withdrawn. The authorized 56-frame calibration uses a low, explicitly provisional threshold; the operator will set the production threshold from the measured peak plus margin.

Sample `vram_free` at submission, during execution, and at completion. Persist samples, per-device minimum free VRAM, the sampled peak used VRAM (`vram_total - minimum vram_free`), its timestamp, change from the submission baseline, and telemetry failures in the job record. These measurements describe the whole worker pool, including other workloads; periodic sampling does not establish a job-exclusive allocation or capture every instantaneous peak.

The 56-frame calibration now has a successful warm run with 225 samples and no telemetry errors: sampled pool peak **119.9887 GiB**, increase from submission **2.7458 GiB**. The earlier cold run observed **120.5918 GiB** peak usage and **31.5339 GiB** increase but failed calibration because of one statistics timeout, despite completing the remote render. Do not use the warm-run delta as the cold loading requirement or call the incomplete cold peak a bound. See [calibration evidence](H3-WORKER-PROFILES.md#post-reboot-calibration-results). The production threshold remains the operator's decision.

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

P0: native setup; the complete LLM adapter; effective residency/admission policy; N-worker registry and HTTP transport with only beastviii configured; durable pinning before upload; worker-bound H3 inspection/validation/test/evidence; VRAM preflight and sampled execution measurements; systemd service on node #6 with Tailscale access, private runtime env file, and durable logs.

P1 must ship before real production renders: only queued/uploading jobs are replayable; declare expected artifacts before submission and verify completeness; never treat `node_errors` plus a partial nonempty map as success; expose postprocessing and success-hook failures in job state. A 60–90 minute render must not be silently resubmitted after an uncertain submission.

P2 is deferred: `/ws` progress (polling is acceptable for v1), cancellation race hardening beyond prompt-specific interrupt, staging/promoting a whole artifact set after partial downloads, and legacy Windows absolute-path migration. The node uses a fresh data root.

The deployment PR will follow existing `kasari-*.service` patterns, use `/opt/kasari/config/` with mode-600 env files and `Restart=on-failure`, and document install/health/log locations in `docs/DEPLOY-CLUSTER.md`. Startup must not erase prior logs, even if a later packaging layer recreates its runtime directory.
