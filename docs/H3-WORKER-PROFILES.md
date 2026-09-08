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

The original unmeasured admission estimate is withdrawn. The operator reports that rebooting node #8 cleared orphaned unified-pool residue; steady state after boot and model load is about 53 GiB free system RAM and 32.6 GiB free VRAM, with zero swap. Those are operator-reported readings, not calibration results.

For the authorized **56-frame, 864×480** calibration only, use:

```dotenv
DS_COMFY_MIN_FREE_VRAM_GIB=1
DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=true
DS_COMFY_MEMORY_SAMPLE_INTERVAL_SEC=1
DS_COMFY_MEMORY_REQUEST_TIMEOUT_SEC=30
```

The 1 GiB threshold is deliberately low to permit the measurement the operator requested. It is **provisional and not a production recommendation**. The operator will set the production threshold from the measured peak plus margin; no production threshold is inferred from serialized weight sizes. Dimensions, frame count, reference count, and graph stages can change the requirement.

The GB10 uses unified memory. `/system_stats` RAM and VRAM describe overlapping capacity but diverge substantially. Admission evaluates **`vram_free` only** for every render device. RAM remains informational; the values are neither added nor averaged. The app does not evict other services, unload their models, or reroute to an unapproved node.

Before POST `/prompt`, the pinned job saves the fresh `/system_stats` snapshot, measured free/total RAM and VRAM, required VRAM threshold, provisional flag, timestamp, worker identity, decision, and failure reason in `memory_admission`. Low or missing VRAM measurements produce a specific failed job with the available and required values. A failed admission is not a successful render test and cannot activate the workflow.

`memory_usage` records submission, interval, and completion samples in the job record. For each device, it reports baseline free VRAM, minimum sampled free VRAM, sampled peak used VRAM (`vram_total - minimum vram_free`), increase from baseline, peak timestamp, and total VRAM. Samples retain their timestamps and RAM context. Any telemetry errors remain visible; incomplete sampling must not be described as a complete measurement.

For periodic execution samples, the interval is a minimum delay **after the preceding statistics request completes**, not a fixed sampling frequency. Request latency lengthens the spacing; submission and completion samples may be closer together. Sample timestamps record actual observations. Statistics requests use the separately configurable 30-second timeout above; a timeout remains an explicit telemetry error and fails the test even if ComfyUI finishes the render.

The reported peak covers the **whole worker pool**, including other workloads; it is not the allocation attributable to this job alone. Periodic sampling can miss peaks shorter than the configured interval. Retain both peak used VRAM and the baseline-to-minimum free VRAM change when reporting the calibration, so the operator can set a margin with those limits in mind.

## Acceptance record

The Settings flow ran locally on macOS against remote beastviii on **2026-09-08 at 04:59 UTC**. The isolated test runtime used the private mode-600 `.tmp/phase4/runtime.env` and `.tmp/phase4/data/` root. It did not alter the saved ComfyUI graph or cluster services.

| Check | Result |
| --- | --- |
| Settings import ID and workflow digest | `imp-5a82d8ed9e4ddd937c3036ef6350110b`; SHA-256 `09fd6877d333efa09c0d79aa7a9eed1cd1a17499b1db44abfcd4fb2ac6e385de` |
| Read-only validation through Settings | Passed against live beastviii metadata; selected H3 75, seed 20, final SaveVideo 46 |
| 56-frame test job ID and admission outcome | `job_2d0e39bb2409` failed admission before POST `/prompt`; prompt ID remains null |
| Remote 56-frame test | `job_cd3e1511d6f6` succeeded on beastviii; selected output downloaded to the app's data directory; 225 memory samples, zero telemetry errors |
| Workflow activation | **Use Workflow** succeeded through Settings; explicit active profile `custom-09fd6877d333efa09c0d79aa7a9eed1c-5864722557769b5c`, no warning, eligible only on beastviii at its configured URL |
| Production H3 render | Pending the operator's production threshold and a production render |
| Backend/frontend tests and CI | 1,234 backend tests and 236 frontend tests pass locally; frontend build passes. Native CI repeats the checks on macOS, Linux x64, and Linux ARM64 in the Phase 4 PR. The existing 16 legacy Windows cases remain deselected; no new quarantine. |

That pre-reboot job measured **11,664,068,608 bytes (10.86 GiB) free RAM** and **5,152,732,104 bytes (4.80 GiB) free VRAM**. It failed against the now-withdrawn placeholder threshold. Sanitized historical evidence remains at `.tmp/phase4/live-test-evidence.json`; `.tmp/phase4/settings-memory-evidence.png` captures the failure and disabled activation after a browser reload. A reference image reached the worker by HTTP before admission; no generation graph was submitted. These readings do not describe the rebooted worker and do not establish an execution peak.

### Post-reboot calibration results

Both executions used the authorized 56-frame SPARK test on the same pinned worker and the provisional 1 GiB admission setting. The cold run loaded the H3 stack; the subsequent warm run reused resident state. These different starting conditions matter when choosing a production threshold.

| Measurement | Cold run `job_126264783711` | Warm run `job_cd3e1511d6f6` |
| --- | --- | --- |
| Job result | Failed: render completed, but one statistics `ReadTimeout` made calibration incomplete | Succeeded with downloaded video |
| Observation window, 2026-09-08 UTC | 15:05:10–15:09:24 | 15:15:56–15:19:59 |
| Successful samples / telemetry errors | 228 / 1 | 225 / 0 |
| VRAM free at submission | 32.6316 GiB | 4.4467 GiB |
| Minimum sampled free VRAM | 1.0977 GiB | 1.7009 GiB |
| Sampled peak used VRAM, whole worker pool | **120.5918 GiB** | **119.9887 GiB** |
| Increase in pool usage from submission | 31.5339 GiB | 2.7458 GiB |
| Peak observation, UTC | 15:08:59.829041 | 15:19:43.409428 |
| Total reported VRAM | 121.6896 GiB | 121.6896 GiB |

The cold run's largest observed pool usage was **129,484,477,780 bytes**, with **1,178,688,172 bytes** free at that observation and a **33,859,224,916-byte** increase from its **35,037,913,088-byte** free baseline. Its timeout means that the true peak may be higher; this run did not certify the workflow or masquerade as a successful test.

The successful warm run's sampled peak was **128,836,891,060 bytes**, with **1,826,274,892 bytes** free and a **2,948,330,876-byte** increase from its **4,774,605,768-byte** free baseline. Its mean observation spacing was approximately **1.083 seconds**, with a longest recorded spacing of **4.649 seconds**. Zero telemetry errors establishes complete recorded sampling for this run, not continuous observation of every instant.

The video loaded in Settings at **864×480**, duration **2.333333 seconds** (56 frames at 24 fps), with browser readiness state 4 and no playback error. The **Use Workflow** action activated the successful evidence, and the API returned the explicit custom profile without a warning. The inspected browser evidence is `.tmp/phase4/reboot/activated-test.png`.

The warm run's 2.7458 GiB change must not be treated as the cold loading requirement. Neither peak isolates this job from other processes sharing the pool. The operator's production threshold remains **pending**; the 1 GiB calibration setting stays explicitly provisional. Durable local records are `.tmp/phase4/data/jobs/job_126264783711/job.json` and `.tmp/phase4/data/jobs/job_cd3e1511d6f6/job.json`.

Only `beastviii` at `http://100.93.117.98:8188` is configured. No second worker is planned, and node #3's former H3 install on port 8189 was deliberately retired. The registry continues to support N workers. The [accepted topology](CLUSTER-PORT-PLAN.md) supersedes the earlier two-worker live acceptance requirement. Systemd installation on node #6, private runtime env installation, Tailscale browser access, and durable service logs remain a separate Phase 5 deployment.
