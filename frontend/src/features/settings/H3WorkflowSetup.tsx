import { useRenderWorkers } from "../../shared/components/RenderWorkers";
import { RenderJobInfo } from "../../shared/components/RenderJobInfo";
import { useEffect, useRef, useState } from "react";
import {
  activateH3Import,
  bindH3ImportWorker,
  h3ProfileFailure,
  fetchH3ImportAnalysis,
  fetchH3Profiles,
  importH3Workflow,
  saveH3Mapping,
  selectH3ImportOutput,
  selectH3Profile,
  selectH3TestOutput,
  testH3Import,
  validateH3Import,
} from "../../shared/api/client";
import type { H3Profiles, H3WorkerBinding } from "../../shared/api/types";
import { useProject } from "../../shared/project/ProjectContext";
import { listLibraryAssets, type LibraryAsset, type LibraryKind } from "../library/api";
import { getH3Job, type H3JobRecord } from "../production/api";
import type {
  H3Analysis,
  H3Candidate,
  H3LifecycleStatus,
  H3Mapping,
  H3TestRun,
} from "./types";

const STORAGE_KEY = "director-studio.h3-setup";
const PICTURE_KINDS: LibraryKind[] = ["actors", "costumes", "scenes", "props", "layouts"];
const STATUS_LABELS: Record<H3LifecycleStatus, string> = {
  draft: "Choose output",
  mapped: "Inputs confirmed",
  validated: "Validated",
  tested: "Tested",
  active: "Active",
};
type Operation = "idle" | "loading" | "importing" | "saving" | "validating" | "testing" | "activating" | "selecting";

function remember(importId: string, worker: H3WorkerBinding | null) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ importId, workerId: worker?.worker_id, workerUrl: worker?.worker_url }));
  } catch {
    // Runtime setup still works when browser storage is unavailable.
  }
}

function remembered(): { importId?: string; workerId?: string; workerUrl?: string } {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function sameWorker(left: { worker_id?: string | null; worker_url?: string | null } | null | undefined, right: H3WorkerBinding | null | undefined): boolean {
  return Boolean(left && right && left.worker_id === right.worker_id && left.worker_url === right.worker_url);
}

function savedWorker(): H3WorkerBinding | null {
  const saved = remembered();
  return saved.workerId && saved.workerUrl ? { worker_id: saved.workerId, worker_url: saved.workerUrl } : null;
}

function nodeLabel(candidate: H3Candidate): string {
  return `${candidate.display_name} — ${candidate.class_type} (Node ${candidate.node_id})`;
}

function mappingFor(analysis: H3Analysis, h3NodeId: string, seedNodeId: string): H3Mapping | null {
  const outputNodeId = analysis.selected_output_node_id || analysis.mapping?.output.node_id;
  if (!outputNodeId || !h3NodeId) return null;
  return {
    inputs: {
      h3_node_id: h3NodeId,
      prompt_input: "prompt",
      width_input: "width",
      height_input: "height",
      frames_input: "length",
      picture_input_pattern: "ref_images.ref_image_{index}",
      audio_input_pattern: "ref_audios.ref_audio_{index}",
      seed_node_id: seedNodeId || null,
      seed_input: seedNodeId ? "noise_seed" : null,
    },
    output: { node_id: outputNodeId, artifact_index: null },
  };
}

export function H3WorkflowSetup({ active = true }: { active?: boolean }) {
  const { projectId } = useProject();
  const { workers, error: workerError } = useRenderWorkers(active);
  const [worker, setWorker] = useState<H3WorkerBinding | null>(savedWorker);
  const [importId, setImportId] = useState(() => remembered().importId || "");
  const workerRef = useRef(worker);
  const revisionRef = useRef(0);
  const workerId = worker?.worker_id || "";
  const configuredWorker = workers?.find((item) => item.id === workerId);
  const endpointChanged = Boolean(worker && configuredWorker && worker.worker_url !== configuredWorker.base_url);
  const workerReady = Boolean(worker && configuredWorker?.status === "up" && !endpointChanged && !workerError);
  const [profiles, setProfiles] = useState<H3Profiles | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [selectedWorkflow, setSelectedWorkflow] = useState("");
  const [analysis, setAnalysis] = useState<H3Analysis | null>(null);
  const [mapping, setMapping] = useState<H3Mapping | null>(null);
  const [mappingDirty, setMappingDirty] = useState(false);
  const [stage, setStage] = useState<H3LifecycleStatus>("draft");
  const [operation, setOperation] = useState<Operation>("loading");
  const [error, setError] = useState<string | null>(null);
  const [pictures, setPictures] = useState<LibraryAsset[]>([]);
  const [voices, setVoices] = useState<LibraryAsset[]>([]);
  const [picture, setPicture] = useState("");
  const [voice, setVoice] = useState("");
  const [test, setTest] = useState<H3TestRun | null>(null);
  const [job, setJob] = useState<H3JobRecord | null>(null);
  const [assetRefresh, setAssetRefresh] = useState(0);
  const [pollVersion, setPollVersion] = useState(0);
  const errorRef = useRef<HTMLDivElement>(null);
  const busy = operation !== "idle";
  const testActive = Boolean(test && (!job || ["queued", "uploading", "running"].includes(job.status)));
  const bindingMatches = sameWorker(analysis?.lifecycle.worker, worker);

  const applyAnalysis = (next: H3Analysis, revision = revisionRef.current) => {
    if (revision !== revisionRef.current) return;
    if (!sameWorker(next.lifecycle.worker, workerRef.current))
      throw new Error(`Import ${next.import_id} is no longer bound to the selected worker. Bind its configured endpoint again.`);
    setAnalysis(next);
    setMapping(next.mapping);
    setMappingDirty(false);
    setStage(next.lifecycle.status);
  };

  async function perform(name: Operation, action: (current: () => boolean) => Promise<void>) {
    const revision = revisionRef.current;
    const current = () => revision === revisionRef.current;
    setOperation(name);
    setError(null);
    try {
      await action(current);
    } catch (err) {
      if (current()) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (current()) setOperation("idle");
    }
  }

  async function changeWorker(next: H3WorkerBinding | null) {
    revisionRef.current += 1;
    workerRef.current = next;
    setWorker(next);
    setJob(null);
    setTest(null);
    setAnalysis(null);
    setMapping(null);
    setMappingDirty(false);
    setStage("draft");
    remember(importId, next);
    if (!importId) { setOperation("idle"); setError(null); return; }
    await perform("loading", async (current) => {
      await bindH3ImportWorker(importId, next);
      if (!current() || !next) return;
      const result = await fetchH3ImportAnalysis(importId, next);
      if (current()) applyAnalysis(result);
    });
  }

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  useEffect(() => {
    let cancelled = false;
    void fetchH3Profiles().then((current) => {
      if (cancelled) return;
      setProfiles(current);
      setSelectedWorkflow(current.selected_profile_id || current.active?.profile_id || "");
      setProfileError(h3ProfileFailure(current));
    }).catch((err) => {
      if (!cancelled) setProfileError(err instanceof Error ? err.message : String(err));
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const saved = remembered();
    const binding = savedWorker();
    const revision = revisionRef.current;
    const current = () => !cancelled && revision === revisionRef.current;
    void (async () => {
      try {
        if (saved.importId && binding) {
          const next = await fetchH3ImportAnalysis(saved.importId, binding);
          if (!current()) return;
          applyAnalysis(next, revision);
          if (sameWorker(next.lifecycle.worker, binding) && next.lifecycle.test_job_id) {
            setTest({ import_id: saved.importId, job_id: next.lifecycle.test_job_id,
              job_url: `/api/h3-ref2va/jobs/${next.lifecycle.test_job_id}`,
              workflow_sha256: next.workflow_sha256, mapping_sha256: next.lifecycle.mapping_sha256 || "",
              status: "queued", ...binding });
          }
        }
      } catch (err) {
        if (current()) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (current()) setOperation("idle");
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!active || !projectId) return;
    Promise.all(
      [...PICTURE_KINDS, "voices" as LibraryKind].map((kind) =>
        listLibraryAssets(kind, projectId, true),
      ),
    )
      .then((groups) => {
        if (cancelled) return;
        setPictures([
          ...new Map(
            groups
              .slice(0, 5)
              .flat()
              .filter((asset) =>
                Object.values(asset.files).some(
                  (file) => file && /\.(png|jpe?g|webp)$/i.test(file),
                ),
              )
              .map((asset) => [asset.id, asset]),
          ).values(),
        ]);
        setVoices(groups[5].filter((asset) => asset.meta?.h3_ready && asset.files.reference));
      })
      .catch((err) => {
        if (!cancelled) setError(`Could not load test assets: ${err instanceof Error ? err.message : String(err)}`);
      });
    return () => { cancelled = true; };
  }, [active, projectId, assetRefresh]);

  useEffect(() => {
    if (!test || !worker || !sameWorker(test, worker) || !workerReady) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let evidenceAttempts = 0;
    const revision = revisionRef.current;
    const current = () => !cancelled && revision === revisionRef.current;
    const poll = async () => {
      try {
        const nextJob = await getH3Job(test.job_id);
        if (!current()) return;
        const unbound = nextJob.worker_id == null && nextJob.worker_url == null;
        const awaitingPin = unbound && ["queued", "uploading"].includes(nextJob.status);
        const failedBeforePin = unbound && ["failed", "cancelled"].includes(nextJob.status);
        // Creation is durable before worker selection. That brief queued state
        // can be polled, but never supplies successful execution evidence.
        if (!sameWorker(nextJob, worker) && !awaitingPin && !failedBeforePin)
          throw new Error(`Test ${test.job_id} has a missing, incomplete, or different worker assignment; its evidence cannot be used here.`);
        setJob(nextJob);
        if (["queued", "uploading", "running"].includes(nextJob.status)) {
          setOperation("testing");
          timer = setTimeout(poll, 2000);
          return;
        }
        if (nextJob.status !== "succeeded") throw new Error(nextJob.error || `Test ${nextJob.status}.`);
        const candidates = Object.keys(nextJob.outputs).filter((key) => key.startsWith("video_candidate_"));
        if (candidates.length > 1) { setOperation("idle"); return; }
        const result = await fetchH3ImportAnalysis(test.import_id, worker);
        if (!current()) return;
        if (!sameWorker(result.lifecycle.worker, worker) || result.lifecycle.test_job_id !== test.job_id)
          throw new Error("Test evidence was invalidated. Validate and test this workflow on the selected worker again.");
        if (!["tested", "active"].includes(result.lifecycle.status)) {
          evidenceAttempts += 1;
          if (evidenceAttempts >= 5) throw new Error("Test completed, but its result is not available yet.");
          timer = setTimeout(poll, 1000);
          return;
        }
        applyAnalysis(result, revision);
        remember(result.import_id, worker);
        setTest(null);
        setOperation("idle");
      } catch (err) {
        if (current()) {
          setError(err instanceof Error ? err.message : String(err));
          setOperation("idle");
        }
      }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [test, pollVersion, worker, workerReady]);

  const outputCandidates = analysis?.output_candidates || [];
  const selectedOutput = analysis?.selected_output_node_id || mapping?.output.node_id || "";
  const h3Candidates = analysis?.h3_candidates || [];
  const seedCandidates = analysis?.seed_candidates || [];
  const testCandidates = job
    ? Object.entries(job.outputs)
        .filter(([key, slot]) => key.startsWith("video_candidate_") && slot.url)
        .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
    : [];
  const canValidate = Boolean(mapping && analysis && !mappingDirty && stage === "mapped" && bindingMatches && workerReady && !busy);
  const canTest = Boolean(mapping && !mappingDirty && picture && ["validated", "tested"].includes(stage) && bindingMatches && workerReady && !busy && !testActive);
  const canActivate = Boolean(analysis && !mappingDirty && stage === "tested" && bindingMatches && workerReady && !busy && !testActive && !error);

  return (
    <div className="h3-workflow-setup" aria-busy={busy}>
      <aside className="workflow-profile-rail section-card" aria-labelledby="active-workflow-title">
        <h2 id="active-workflow-title" className="section-card-title">Current Workflow</h2>
        {profileError ? <div className="banner error" role="alert">{profileError}<p>Choose an installed workflow explicitly to recover, or repair the selected custom workflow below.</p></div> : null}
        {profiles ? <>
          {profiles.active ? <>
            <strong className="workflow-profile-name">{profiles.active.display_name}</strong>
            <p className="field-hint">{profiles.active.selection_message || (profiles.active.selection_source === "initial_default" ? "No workflow has been selected yet. The shipped H3 workflow is the initial default." : "H3 render workers use this selected ComfyUI workflow.")}</p>
            <details><summary>Workflow identity</summary><code className="workflow-hash">{profiles.active.workflow_sha256}</code>
              {profiles.active.eligible_workers?.map((item) => <p key={`${item.worker_id}:${item.worker_url}`}>Tested worker: {item.worker_id} · <code>{item.worker_url}</code></p>)}
            </details>
          </> : <strong>Selected workflow unavailable: {profiles.selected_profile_id || "unknown"}</strong>}
          <label className="field">
            <span>Installed workflow</span>
            <select value={selectedWorkflow} disabled={busy} onChange={(event) => setSelectedWorkflow(event.target.value)}>
              {selectedWorkflow && !profiles.profiles.some((item) => item.profile_id === selectedWorkflow) ? <option value={selectedWorkflow}>{selectedWorkflow} — unavailable</option> : null}
              {profiles.profiles.map((item) => <option key={item.profile_id} value={item.profile_id}>{item.display_name}</option>)}
            </select>
          </label>
          <button type="button" className="btn secondary" disabled={busy || !selectedWorkflow || (!profileError && selectedWorkflow === profiles.active?.profile_id && profiles.active.selection_source !== "initial_default")} onClick={() => void perform("selecting", async (current) => {
            await selectH3Profile(selectedWorkflow);
            const result = await fetchH3Profiles();
            if (!current()) return;
            setProfiles(result);
            setProfileError(h3ProfileFailure(result));
            setSelectedWorkflow(result.selected_profile_id || result.active?.profile_id || "");
          })}>Use Workflow</button>
        </> : !profileError ? <p className="field-hint">Loading active workflow…</p> : null}
      </aside>

      <div className="workflow-setup-main">
        {error ? <div className="banner error" role="alert" tabIndex={-1} ref={errorRef}>{error}</div> : null}
        <section className="section-card" aria-labelledby="custom-h3-title">
          <h2 id="custom-h3-title" className="section-card-title">Custom H3 Workflows</h2>
          <p className="field-hint">Import a ComfyUI API JSON. Director Studio leaves the internal graph unchanged and only connects its input and final-video boundaries.</p>
          <label className="field"><span>Worker for discovery and test</span>
            <select aria-label="Worker for discovery and test" value={workerId} disabled={busy && operation !== "testing"} onChange={(event) => {
              const chosen = workers?.find((item) => item.id === event.target.value);
              void changeWorker(chosen ? { worker_id: chosen.id, worker_url: chosen.base_url } : null);
            }}>
              <option value="">Choose a render worker…</option>
              {workerId && workers && !workers.some((item) => item.id === workerId) ? <option value={workerId}>{workerId} — no longer configured</option> : null}
              {(workers || []).map((item) => <option key={item.id} value={item.id}>{item.id} — {item.status}</option>)}
            </select>
          </label>
          {worker ? <p className="field-hint">Selected worker: {worker.worker_id} · <code>{worker.worker_url}</code></p> : null}
          {workerError ? <div className="banner error" role="alert">{workerError}</div> : null}
          {configuredWorker?.error ? <div className="banner error">{configuredWorker.error}</div> : null}
          {endpointChanged ? <div className="banner error" role="alert">The configured endpoint for {workerId} changed to {configuredWorker?.base_url}. Existing validation and test evidence cannot authorize this endpoint.</div> : null}
          {worker && configuredWorker && (endpointChanged || (!analysis && importId && !busy)) ? <button type="button" className="btn secondary" disabled={busy || configuredWorker.status !== "up"} onClick={() => void changeWorker({ worker_id: configuredWorker.id, worker_url: configuredWorker.base_url })}>Bind configured endpoint</button> : null}
          {analysis?.lifecycle.invalidation_reason ? <div className="banner" role="status">{analysis.lifecycle.invalidation_reason}</div> : null}
          <p className="field-hint">Metadata, validation and the test render use this worker. Changing or clearing it invalidates validation and test evidence. Existing test jobs stay assigned to their original worker.</p>
          <label className="field"><span>Import Workflow</span><input type="file" accept=".json,application/json" disabled={busy || !workerReady} onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file && worker) {
              revisionRef.current += 1;
              void perform("importing", async (current) => {
                setAnalysis(null); setMapping(null); setJob(null); setTest(null); setStage("draft");
                const imported = await importH3Workflow(file);
                if (!current()) return;
                setImportId(imported.import_id);
                remember(imported.import_id, worker);
                await bindH3ImportWorker(imported.import_id, worker);
                if (!current()) return;
                const result = await fetchH3ImportAnalysis(imported.import_id, worker);
                if (current()) applyAnalysis(result);
              });
            }
          }} /></label>
          {analysis ? <>
            {analysis.issues.map((issue, index) => <p className="field-hint" key={`${issue.code}-${index}`}>{issue.message}</p>)}
            {analysis.fixed_dependencies.length ? <details><summary>Workflow-owned files</summary>{analysis.fixed_dependencies.map((item) => <p key={`${item.node_id}-${item.input_name}`}>{item.value}</p>)}</details> : null}
          </> : <p className="empty-copy">Choose the API-format JSON exported by ComfyUI.</p>}
        </section>

        <section className="section-card" aria-labelledby="output-title">
          <h2 id="output-title" className="section-card-title">1. Final video output</h2>
          <p className="field-hint">Choose the terminal node whose video Director Studio should keep. Node name is shown first; ID is secondary.</p>
          {analysis ? <label className="field"><span>Final video node</span><select aria-label="Final video node" value={selectedOutput} disabled={busy || !workerReady || !bindingMatches} onChange={(event) => worker && void perform("selecting", async (current) => {
            const next = await selectH3ImportOutput(analysis.import_id, event.target.value, worker);
            if (current()) { applyAnalysis(next); setJob(null); setTest(null); }
          })}><option value="">Choose a final video node…</option>{outputCandidates.map((candidate) => <option key={candidate.node_id} value={candidate.node_id}>{nodeLabel(candidate)}</option>)}</select></label> : null}
        </section>

        <section className="section-card" aria-labelledby="inputs-title">
          <h2 id="inputs-title" className="section-card-title">2. H3 Inputs</h2>
          <p className="field-hint">After output selection, upstream H3 and optional seed nodes are discovered by reverse traversal.</p>
          {analysis && selectedOutput ? <>
            <label className="field"><span>H3 generation node</span><select aria-label="H3 generation node" value={mapping?.inputs.h3_node_id || ""} disabled={busy} onChange={(event) => {
              const next = mappingFor(analysis, event.target.value, mapping?.inputs.seed_node_id || "");
              setMapping(next); setMappingDirty(true); setStage("mapped");
            }}><option value="">Choose the H3 node…</option>{h3Candidates.map((candidate) => <option key={candidate.node_id} value={candidate.node_id}>{nodeLabel(candidate)}</option>)}</select></label>
            <label className="field"><span>Seed node (optional)</span><select aria-label="Seed node (optional)" value={mapping?.inputs.seed_node_id || ""} disabled={busy || !mapping} onChange={(event) => {
              if (!mapping) return;
              setMapping({ ...mapping, inputs: { ...mapping.inputs, seed_node_id: event.target.value || null, seed_input: event.target.value ? "noise_seed" : null } });
              setMappingDirty(true); setStage("mapped");
            }}><option value="">Use workflow seed settings</option>{seedCandidates.map((candidate) => <option key={candidate.node_id} value={candidate.node_id}>{nodeLabel(candidate)}</option>)}</select></label>
            {mappingDirty ? <p className="field-hint">Confirm changed input nodes before validating.</p> : null}
            {mapping ? <div className="workflow-dependencies"><strong>Connected inputs</strong><p>Prompt, width, height, frames · Picture 1–9 · Audio 1–3 · optional seed</p></div> : null}
            <button type="button" className="btn secondary" disabled={busy || !mapping || !workerReady || !bindingMatches} onClick={() => worker && void perform("saving", async (current) => {
              await saveH3Mapping(analysis.import_id, mapping!, worker);
              if (!current()) return;
              const result = await fetchH3ImportAnalysis(analysis.import_id, worker);
              if (!current()) return;
              applyAnalysis(result); setJob(null); setTest(null);
              remember(analysis.import_id, worker);
            })}>Confirm input nodes</button>
          </> : <p className="empty-copy">Choose the final video output first.</p>}
        </section>

        <section className="section-card" aria-labelledby="test-title">
          <div className="section-card-head"><h2 id="test-title" className="section-card-title">3. Validate &amp; Test</h2><span role="status" aria-live="polite">{busy ? `${operation}…` : STATUS_LABELS[stage]}</span></div>
          <p className="field-hint">ComfyUI validates the graph, then a 56-frame test confirms the selected boundary. Multiple videos can be previewed and selected without rerunning.</p>
          {analysis?.lifecycle.worker ? <details><summary>Worker evidence</summary>
            <p>{analysis.lifecycle.worker.worker_id} · <code>{analysis.lifecycle.worker.worker_url}</code></p>
            {analysis.lifecycle.inspected_at ? <p>Metadata inspected: {analysis.lifecycle.inspected_at}</p> : null}
            {analysis.lifecycle.metadata_sha256 ? <code className="workflow-hash">{analysis.lifecycle.metadata_sha256}</code> : null}
            {analysis.lifecycle.validated_at ? <p>Validated: {analysis.lifecycle.validated_at}</p> : null}
          </details> : null}
          <button type="button" className="btn secondary" disabled={!canValidate} onClick={() => analysis && worker && void perform("validating", async (current) => {
            await validateH3Import(analysis.import_id, worker);
            if (!current()) return;
            const result = await fetchH3ImportAnalysis(analysis.import_id, worker);
            if (current()) applyAnalysis(result);
          })}>Validate with ComfyUI</button>
          <div className="workflow-test-assets">
            <label className="field"><span>Picture for test</span><select value={picture} disabled={busy} onChange={(event) => setPicture(event.target.value)}><option value="">Choose one Picture…</option>{pictures.map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}</select></label>
            <label className="field"><span>Voice for test (optional standalone Audio)</span><select value={voice} disabled={busy || !mapping?.inputs.audio_input_pattern} onChange={(event) => setVoice(event.target.value)}><option value="">No Audio reference</option>{voices.map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}</select></label>
          </div>
          <div className="actions">
            <button type="button" className="btn secondary" disabled={!projectId || busy} onClick={() => setAssetRefresh((value) => value + 1)}>Refresh assets</button>
            <button type="button" className="btn secondary" disabled={!canTest} onClick={() => analysis && worker && void perform("testing", async (current) => {
              setJob(null);
              const run = await testH3Import(analysis.import_id, picture, voice || null, worker);
              if (!current()) return;
              remember(analysis.import_id, worker); setTest({ ...run, ...worker }); setStage("validated");
            })}>Run 56-frame test</button>
          </div>
          <RenderJobInfo job={job} />
          {testCandidates.length ? <div className="workflow-test-result"><strong>{testCandidates.length > 1 ? "Choose the final test video" : "Test video"}</strong>{testCandidates.map(([key, slot], index) => <div key={key} className="workflow-test-candidate"><video className="h3-preview" aria-label={`Workflow test video ${index + 1}`} controls preload="metadata" src={slot.url || undefined} />{testCandidates.length > 1 ? <button type="button" className="btn secondary" disabled={busy || !workerReady || !bindingMatches} onClick={() => analysis && worker && void perform("selecting", async (current) => {
                await selectH3TestOutput(analysis.import_id, index, worker);
                if (!current()) return;
                const result = await fetchH3ImportAnalysis(analysis.import_id, worker);
                if (!current()) return;
                applyAnalysis(result);
                remember(analysis.import_id, worker); setTest(null);
              })}>Use video {index + 1}</button> : null}</div>)}</div> : null}
          <div className="actions"><button type="button" className="btn primary" disabled={!canActivate} onClick={() => analysis && worker && void perform("activating", async (current) => {
            const result = await activateH3Import(analysis.import_id, worker);
            if (!current()) return;
            const next = await fetchH3Profiles();
            if (!current()) return;
            setProfiles(next); setProfileError(h3ProfileFailure(next)); setSelectedWorkflow(result.active.profile_id); setStage("active");
          })}>Use Workflow</button>{test && error ? <button type="button" className="btn secondary" onClick={() => { setError(null); setPollVersion((value) => value + 1); }}>Check test status</button> : null}</div>
        </section>
      </div>
    </div>
  );
}
