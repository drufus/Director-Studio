import type { H3ActiveProfile, H3Profiles, H3WorkerBinding, PipelineInfo, RenderWorkerStatus } from "./types";
import type { H3Analysis, H3Import, H3Lifecycle, H3Mapping, H3TestRun, H3Validation } from "../../features/settings/types";

export async function parseError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data.message === "string") {
      const issues = data.details?.issues;
      return [data.message, ...(Array.isArray(issues) ? issues.map((issue: { message?: string }) => issue.message).filter(Boolean) : [])].join("; ");
    }
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join("; ");
    }
    return JSON.stringify(data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

export async function fetchHealth(): Promise<{
  ok: boolean;
  comfy_reachable: boolean;
  comfy_error: string | null;
}> {
  const res = await fetch("/api/health");
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function fetchPipelines(): Promise<PipelineInfo[]> {
  const res = await fetch("/api/pipelines");
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function fetchRenderWorkers(): Promise<{ workers: RenderWorkerStatus[] }> {
  const res = await fetch("/api/comfy/workers");
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

const H3_PROFILES = "/api/workflow-profiles/h3";
async function profileRequest<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const res = await fetch(`${H3_PROFILES}${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}
const importPath = (id: string, action: string) => `/imports/${encodeURIComponent(id)}/${action}`;
const workerQuery = (worker: H3WorkerBinding) => `?${new URLSearchParams({ worker_id: worker.worker_id, worker_url: worker.worker_url })}`;
export const fetchH3Profiles = () => profileRequest<H3Profiles>();
export async function importH3Workflow(file: File): Promise<H3Import> {
  const body = new FormData();
  body.append("workflow", file);
  const res = await fetch(`${H3_PROFILES}/imports`, { method: "POST", body });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}
export const bindH3ImportWorker = (id: string, worker: H3WorkerBinding | null) => profileRequest<{ worker: H3WorkerBinding | null; lifecycle: H3Lifecycle }>(importPath(id, "worker"), "PUT", worker || { worker_id: null, worker_url: null });
export const fetchH3ImportAnalysis = (id: string, worker: H3WorkerBinding) => profileRequest<H3Analysis>(importPath(id, "analysis") + workerQuery(worker));
export const selectH3ImportOutput = (id: string, nodeId: string, worker: H3WorkerBinding) => profileRequest<H3Analysis>(importPath(id, "output") + workerQuery(worker), "PUT", { node_id: nodeId });
export const saveH3Mapping = (id: string, mapping: H3Mapping, worker: H3WorkerBinding) => profileRequest<{ import_id: string; mapping: H3Mapping }>(importPath(id, "mapping") + workerQuery(worker), "PUT", mapping);
export const validateH3Import = (id: string, worker: H3WorkerBinding) => profileRequest<H3Validation>(importPath(id, "validate") + workerQuery(worker), "POST");
export const testH3Import = (id: string, pictureAssetId: string, audioAssetId: string | null, worker: H3WorkerBinding) => profileRequest<H3TestRun>(importPath(id, "test"), "POST", { picture_asset_id: pictureAssetId, audio_asset_id: audioAssetId, ...worker });
export const selectH3TestOutput = (id: string, artifactIndex: number, worker: H3WorkerBinding) => profileRequest<{ import_id: string; artifact_index: number; job_id: string; status: "succeeded" }>(importPath(id, "test-output") + workerQuery(worker), "PUT", { artifact_index: artifactIndex });
export const activateH3Import = (id: string, worker: H3WorkerBinding) => profileRequest<{ import_id: string; profile_id: string; active: H3ActiveProfile }>(importPath(id, "activate") + workerQuery(worker), "POST");
export const selectH3Profile = (profileId: string) => profileRequest<{ active: H3ActiveProfile }>("/select", "POST", { profile_id: profileId });

export function h3ProfileFailure(profiles: H3Profiles): string | null {
  if (profiles.active_error) {
    const identity = profiles.selected_profile_id || profiles.active_error.details?.profile_id;
    return `${identity ? `Selected workflow ${identity}: ` : ""}${profiles.active_error.message}`;
  }
  if (!profiles.active) return "No active H3 workflow is available. Choose a workflow in Settings → Workflows.";
  if (profiles.active.source === "custom" && !profiles.active.eligible_workers?.length)
    return `Selected workflow ${profiles.active.profile_id} has no eligible render workers. Validate and test it in Settings → Workflows.`;
  return null;
}
