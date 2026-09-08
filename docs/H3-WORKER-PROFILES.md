# H3 workflows on remote workers

Phase 4 binds H3 workflow inspection, validation, test results, and activation to an exact render worker ID and URL. A custom workflow can run only on workers with matching evidence. The runtime importer preserves the graph's internal models and processing; it does not rewrite the bundled workflow or the pipeline `workflow.py` contracts.

## Import and verify a workflow

Use a genuine ComfyUI **API-format export**, then open:

```text
Settings → Workflows → H3
→ choose the worker → Import Workflow
→ Final video output → H3 Inputs → Confirm input nodes
→ Validate with ComfyUI → Run 56-frame test → Use Workflow
```

Choose the worker explicitly, even when only one is configured. The browser submits its ID and the URL shown in the configured fleet. The server rejects a removed worker or a changed URL before contacting it. Importing saves the JSON as an unbound draft; a separate binding mutation records the chosen worker. Reading analysis never silently changes that binding.

Select the terminal whose video you want to retain, then confirm the upstream `MiniMaxH3ReferenceToVideo` and optional seed node. The existing boundary guards remain: no `ref_frame` or `last_frame` on the selected H3 boundary, no substitution with `MiniMaxH3ImageToVideo`, and no global exactly-one-node rule. `VHS_VideoCombine` terminals and unrelated graph branches remain supported.

Director Studio injects prompt, dimensions, frame count, Picture references, optional standalone Audio references, output prefix, and an optional seed through the confirmed boundary. It uploads references to the selected worker over HTTP and downloads the selected terminal's outputs into the app's `data/`. No shared disk or local ComfyUI install is required. When the test terminal emits multiple videos, select the desired artifact after previewing them; that selection does not rerun the graph.

## Evidence, invalidation, and worker eligibility

The external `data/workflow_profiles/h3/` store contains imports, installed profiles, and the explicit `active.json` selection. Import evidence ties together:

- The exact worker ID and normalized URL, plus a binding generation.
- The imported workflow bytes, confirmed mapping, and selected output identity.
- A digest of relevant `/object_info` input/output schemas and the inspection timestamp.
- Successful dependency validation and the matching test job, selected artifact, and test result.

Changing or clearing the worker invalidates its inspection, validation, and test records. Changing the output or mapping invalidates dependent evidence. A relevant metadata change requires fresh validation and testing. Returning from worker A to B and back to A does not resurrect A's earlier test; the binding generation distinguishes those visits. Late responses and test completion hooks must match the current binding and evidence.

A reload restores current test progress from the server lifecycle, including a test awaiting artifact selection. Browser storage remembers the import and worker identity; it cannot confer tested status. The setup UI shows the worker ID, URL, and evidence. A down, removed, or reconfigured worker cannot authorize activation.

Installed custom profiles record eligible ID/URL pairs with separate proofs for each worker. Verifying the same workflow and boundary on another configured worker can add its proof; one worker's result does not certify the rest of the fleet. Scheduling selects from configured, healthy workers that match the captured eligibility. With no eligible worker, the job fails explicitly. After selection, uploads, submission, history, output retrieval, cancellation, and restart recovery stay pinned to that worker.

Each submitted job captures immutable workflow, mapping, and worker-proof snapshots. A later profile selection applies to future jobs. Existing jobs retain their captured identity; missing or altered snapshots fail rather than reconstructing a different workflow.

## Selection failures and recovery

A fresh data root with no saved selection uses **Built-in Official H3** as an initial default and says so. This is distinct from a broken existing selection.

If an explicitly selected custom profile is missing, changed, or invalid, profile status returns `active: null`, the requested identity, and `active_error`. Both Production interfaces stop Comfy submission when the active workflow refresh fails. The Settings import editor remains usable while the active selection is broken.

Repair and reverify the custom workflow, or explicitly select **Built-in Official H3** under Installed workflow and click **Use Workflow**. There is no automatic fallback to the bundled graph. Selecting the built-in does not guarantee that its model dependencies exist on the worker; normal dependency, submission, and memory checks still apply.

## What read-only validation proves

**Validate with ComfyUI** reads `/object_info`; it does not queue a render. It checks installed classes, required inputs, and advertised selector/model options, including Comfy V3 flattened Autogrow inputs, V3 `COMBO` options, and selected nested DynamicCombo inputs. For example, `values.a` satisfies a required Autogrow child without a parent `values` input, while selecting `codec.encoding=re-encode` requires `codec.encoding.crf`.

The metadata digest covers graph-relevant schemas. Changing upload filenames in the worker's `LoadImage`/`LoadAudio` catalog does not invalidate an otherwise identical workflow. The read-only check does not execute custom node validators, load model weights, prove memory sufficiency, or promise a successful render. ComfyUI validates execution at POST `/prompt`; the 56-frame test provides execution and artifact evidence only after it completes successfully.

The dynamic input interpretation follows [ComfyUI's V3 input implementation](https://github.com/Comfy-Org/ComfyUI/blob/v0.30.2/comfy_api/latest/_io.py). Director Studio does not import or evaluate that server code locally.

## SPARK workflow and memory admission

The selected `minimax_h3_ref2va_SPARK.json` graph was exported through ComfyUI's frontend API exporter: **39 UI nodes become 29 API nodes**. The export preserves these boundaries and settings:

| Role | Exported graph identity |
| --- | --- |
| Final video | `SaveVideo`, node `46` |
| H3 generation | `MiniMaxH3ReferenceToVideo`, node `75` |
| Seed | `RandomNoise`, node `20` |
| Sampling | `res_multistep`, 20 steps |
| Checkpoint | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| Text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |

The four deployed weight files total **39.5538 GiB**: checkpoint **19.5302 GiB**, text encoder **14.6098 GiB**, video VAE **4.8501 GiB**, and audio VAE **0.5637 GiB**. File sizes are a planning input, not a measurement of loaded model memory.

For the initial **56-frame, 864×480** validation, use these conservative admission settings:

```dotenv
DS_COMFY_MIN_FREE_RAM_GIB=64
DS_COMFY_MIN_FREE_VRAM_GIB=64
```

This estimate leaves approximately **24.45 GiB** above serialized weight sizes for loading and computation. It is **not a measured peak or a guarantee**. Dequantization, activations, reference count, dimensions, frame count, and other graph stages can change the requirement. Reassess headroom for production-length jobs using measured execution evidence.

The GB10 uses unified memory: the RAM and VRAM figures describe overlapping capacity, so the two 64 GiB checks do **not** require 128 GiB combined. Both measurements must independently satisfy their threshold. The recent node #8 readings of approximately **10.9 GiB free RAM** and **4.8 GiB free VRAM** are below these settings. That headroom blocks admission; it does not justify evicting unrelated services, unloading their models, reducing thresholds silently, or rerouting to an unapproved node.

Before POST `/prompt`, the pinned job saves the fresh `/system_stats` snapshot, measured free/total RAM and VRAM, required thresholds, timestamp, worker identity, decision, and failure reason in `memory_admission`. Low or missing measurements produce a specific failed job with the available and required values. A failed admission is not a successful render test and cannot activate the workflow.

## Acceptance record

The Settings flow ran locally on macOS against remote beastviii on **2026-09-08 at 04:59 UTC**. The isolated test runtime used the private mode-600 `.tmp/phase4/runtime.env` and `.tmp/phase4/data/` root. It did not alter the saved ComfyUI graph or cluster services.

| Check | Result |
| --- | --- |
| Settings import ID and workflow digest | `imp-5a82d8ed9e4ddd937c3036ef6350110b`; SHA-256 `09fd6877d333efa09c0d79aa7a9eed1cd1a17499b1db44abfcd4fb2ac6e385de` |
| Read-only validation through Settings | Passed against live beastviii metadata; selected H3 75, seed 20, final SaveVideo 46 |
| 56-frame test job ID and admission outcome | `job_2d0e39bb2409` failed admission before POST `/prompt`; prompt ID remains null |
| Remote test render and activation | Pending sufficient admitted headroom and successful artifacts |
| Production H3 render | Pending successful workflow activation and production admission |
| Backend/frontend tests and CI | 1,196 backend tests and 233 frontend tests pass locally; frontend build passes. Native CI repeats the checks on macOS, Linux x64, and Linux ARM64 in the Phase 4 PR. The existing 16 legacy Windows cases remain deselected; no new quarantine. |

The job measured **11,664,068,608 bytes (10.86 GiB) free RAM** and **5,152,732,104 bytes (4.80 GiB) free VRAM**, each below **68,719,476,736 bytes (64 GiB)**. The saved `memory_admission` record includes both measurements, totals, required thresholds, worker ID/URL, and timestamp. Sanitized local evidence is `.tmp/phase4/live-test-evidence.json`; `.tmp/phase4/settings-memory-evidence.png` captures the failure and disabled activation after a browser reload. A reference image reached the worker by HTTP before admission; no generation graph was submitted. Capacity must become available before the same validated import can obtain execution proof and activate.

Only `beastviii` at `http://100.93.117.98:8188` is configured. The [accepted topology](CLUSTER-PORT-PLAN.md) remains in force. Live acceptance on each of two workers is **deferred, not dropped**, until the operator enables a second suitable worker. Systemd installation on node #6, private runtime env installation, Tailscale browser access, and durable service logs remain a separate Phase 5 deployment.
