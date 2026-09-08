# Accepted cluster port decisions

Phase 1 supplies native macOS/Linux build, startup, and tests. Subsequent phases remain separate PRs; this file records the accepted constraints and deferred work so native startup is not mistaken for completed cluster inference.

## Runtime and transport

- One Director process on node #6. Worker concurrency belongs inside that process; replicated API services and durable distributed task claiming are out of scope.
- Use direct HTTP for the cluster's ComfyUI transport. Remove comfy-mcp/comfy-cli from that path when the transport phase lands; no dual transport is required for a hypothetical single-box deployment.
- Design for both configured render workers. An unavailable worker stays configured and visibly down. Never contact the protected production ComfyUI instance on beastiii port 8188; its permitted H3 instance uses port 8189.
- Persist worker ownership before upload. Submission, history, output downloads, evidence, and cancellation stay on that worker; no silent substitution of another worker, backend, model, or workflow.

## LLM and model selection

Before adapter implementation, authenticate to the existing LiteLLM router using its host-side env file. Never print, log, paste, or commit the key. Probe each candidate for actual image support, JSON-schema constraint behavior, and stable native tool-call IDs. A model name or catalog entry is not a capability guarantee. Report the results before finalizing the adapter design.

No first-entry model auto-selection/persistence. Report blank selection as blank; keep the picker usable while chat is disabled. Provider-scope persisted selections so a legacy Ollama tag cannot override the new provider configuration. Keep the current picker and model-selection persistence concept while deliberately updating their tests.

Preserve deterministic parsing and the existing bounded, same-model JSON repair paths for planning, storyboard verdicts, six-section prompts, and visual briefs. Document each preserved repair in its implementation PR. Remove image-to-text retries, tool-protocol switching after XML failures, and nonstream regeneration after partial streaming; surface those failures instead.

If no candidate passes a vision probe, explicitly disable visual-direction and layout-analysis features with a clear UI explanation. Do not silently omit images or skip analysis.

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

P0: native setup; the complete LLM adapter; effective residency/admission policy; worker registry and HTTP transport; durable pinning before upload; worker-bound H3 inspection/validation/test/evidence; systemd service on node #6 with Tailscale access, private runtime env file, and durable logs.

P1 must ship before real production renders: only queued/uploading jobs are replayable; declare expected artifacts before submission and verify completeness; never treat `node_errors` plus a partial nonempty map as success; expose postprocessing and success-hook failures in job state. A 60–90 minute render must not be silently resubmitted after an uncertain submission.

P2 is deferred: `/ws` progress (polling is acceptable for v1), cancellation race hardening beyond prompt-specific interrupt, staging/promoting a whole artifact set after partial downloads, and legacy Windows absolute-path migration. The node uses a fresh data root.

The deployment PR will follow existing `kasari-*.service` patterns, use `/opt/kasari/config/` with mode-600 env files and `Restart=on-failure`, and document install/health/log locations in `docs/DEPLOY-CLUSTER.md`. Startup must not erase prior logs, even if a later packaging layer recreates its runtime directory.
