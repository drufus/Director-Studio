// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { H3WorkflowSetup } from "./H3WorkflowSetup";

vi.mock("../../shared/project/ProjectContext", () => ({
  useProject: () => ({ projectId: "project-1" }),
}));

const candidate = (node_id: string, display_name: string, class_type: string) => ({
  node_id,
  display_name,
  class_type,
  title: display_name,
  object_display_name: class_type,
  terminal: class_type === "VHS_VideoCombine",
  output_node: class_type === "VHS_VideoCombine",
  output_types: class_type === "VHS_VideoCombine" ? ["VHS_FILENAMES"] : ["CONDITIONING"],
});

const mapping = {
  inputs: {
    h3_node_id: "136",
    prompt_input: "prompt",
    width_input: "width",
    height_input: "height",
    frames_input: "length",
    picture_input_pattern: "ref_images.ref_image_{index}",
    audio_input_pattern: "ref_audios.ref_audio_{index}",
    seed_node_id: "129",
    seed_input: "noise_seed",
  },
  output: { node_id: "214", artifact_index: null },
};

const active = {
  profile_id: "builtin-official-h3",
  display_name: "Built-in Official H3",
  source: "builtin",
  workflow_sha256: "official-hash",
  contract_version: 2,
  warning: null,
};

const workerA = { worker_id: "worker-a", worker_url: "http://worker-a:8188" };
const workerB = { worker_id: "worker-b", worker_url: "http://worker-b:8188" };
let binding: typeof workerA | null = workerA;
let invalidationReason: string | null = null;
let pendingTestId: string | null = null;
let fleet = [workerA, workerB].map((worker) => ({ id: worker.worker_id, base_url: worker.worker_url, status: "up", error: null, queued_jobs: 0, running_jobs: 0, checked_at: null }));
let selectedOutput = false;
let lifecycleStatus = "draft";
let jobOutputs: Record<string, { url: string }> = {};
let requests: { url: string; init?: RequestInit }[] = [];

function analysis() {
  return {
    import_id: "imp-1",
    workflow_sha256: "workflow-hash",
    selected_output_node_id: selectedOutput ? "214" : null,
    compatibility: selectedOutput ? "auto_compatible" : "needs_confirmation",
    mapping: selectedOutput ? mapping : null,
    output_candidates: [
      candidate("214", "Final Video Combine", "VHS_VideoCombine"),
      candidate("300", "Preview Video", "VHS_VideoCombine"),
    ],
    h3_candidates: selectedOutput ? [candidate("136", "Main H3 Generator", "MiniMaxH3ReferenceToVideo")] : [],
    seed_candidates: selectedOutput ? [candidate("129", "Generation Seed", "RandomNoise")] : [],
    fixed_dependencies: [],
    issues: [],
    lifecycle: {
      status: lifecycleStatus,
      workflow_sha256: "workflow-hash",
      mapping_sha256: selectedOutput ? "mapping-hash" : null,
      validated_at: lifecycleStatus === "draft" || lifecycleStatus === "mapped" ? null : "now",
      test_job_id: lifecycleStatus === "tested" ? "job-1" : pendingTestId,
      worker: binding,
      invalidation_reason: invalidationReason,
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  binding = workerA;
  invalidationReason = null;
  pendingTestId = null;
  fleet = [workerA, workerB].map((worker) => ({ id: worker.worker_id, base_url: worker.worker_url, status: "up", error: null, queued_jobs: 0, running_jobs: 0, checked_at: null }));
  selectedOutput = false;
  lifecycleStatus = "draft";
  jobOutputs = {};
  requests = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    const pathname = url.split("?")[0];
    let body: unknown;
    if (url === "/api/comfy/workers") body = { workers: fleet };
    else if (url === "/api/workflow-profiles/h3") body = { active, profiles: [{ ...active, status: "active" }] };
    else if (url.startsWith("/api/library?")) body = url.includes("kind=voices") ? [{ id: "voice-1", name: "Mia voice", files: { reference: "voice.wav" }, meta: { h3_ready: true } }] : [{ id: "picture-1", name: "Mia portrait", files: { master: "mia.png" }, meta: {} }];
    else if (pathname.endsWith("/imports")) body = { import_id: "imp-1", workflow_sha256: "workflow-hash", filename: "custom.api.json" };
    else if (pathname.endsWith("/worker")) {
      const next = JSON.parse(String(init?.body));
      if (next.worker_id !== binding?.worker_id || next.worker_url !== binding?.worker_url) {
        lifecycleStatus = selectedOutput ? "mapped" : "draft";
        invalidationReason = "Worker binding changed; validate and test again.";
      }
      binding = next.worker_id ? next : null;
      body = { worker: binding, lifecycle: analysis().lifecycle };
    }
    else if (pathname.endsWith("/output")) { selectedOutput = true; body = analysis(); }
    else if (pathname.endsWith("/mapping")) { lifecycleStatus = "mapped"; body = { import_id: "imp-1", mapping }; }
    else if (pathname.endsWith("/validate")) { lifecycleStatus = "validated"; body = { valid: true }; }
    else if (pathname.endsWith("/test-output")) { lifecycleStatus = "tested"; body = { import_id: "imp-1", artifact_index: 1, job_id: "job-1", status: "succeeded" }; }
    else if (pathname.endsWith("/test")) body = { import_id: "imp-1", job_id: "job-1", job_url: "/api/h3-ref2va/jobs/job-1", workflow_sha256: "workflow-hash", mapping_sha256: "mapping-hash", status: "queued", ...binding };
    else if (pathname.endsWith("/analysis")) body = analysis();
    else if (url.includes("/jobs/job-1")) body = { id: "job-1", status: "succeeded", outputs: jobOutputs, error: null, ...workerA };
    else if (pathname.endsWith("/activate")) body = { profile_id: "custom-1", active: { ...active, profile_id: "custom-1", display_name: "My H3 Workflow", source: "custom" } };
    else if (pathname.endsWith("/select")) body = { active };
    else throw new Error(`Unexpected request ${url}`);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("uses workflow language and does not expose agent setup controls", async () => {
  render(<H3WorkflowSetup />);
  expect(await screen.findByRole("complementary", { name: "Current Workflow" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Custom H3 Workflows" })).toBeTruthy();
  expect(screen.queryByText(/profile/i)).toBeNull();
  expect(screen.queryByRole("button", { name: /suggest/i })).toBeNull();
});

it("selects output first and shows node names before IDs", async () => {
  render(<H3WorkflowSetup />);
  fireEvent.change(await screen.findByLabelText("Worker for discovery and test"), { target: { value: "worker-a" } });
  const file = new File(["{}"], "custom.api.json", { type: "application/json" });
  fireEvent.change(await screen.findByLabelText("Import Workflow"), { target: { files: [file] } });
  const output = await screen.findByLabelText("Final video node");
  expect(screen.getByRole("option", { name: "Final Video Combine — VHS_VideoCombine (Node 214)" })).toBeTruthy();
  fireEvent.change(output, { target: { value: "214" } });
  expect(await screen.findByRole("option", { name: "Main H3 Generator — MiniMaxH3ReferenceToVideo (Node 136)" })).toBeTruthy();
  expect(screen.getByRole("option", { name: "Generation Seed — RandomNoise (Node 129)" })).toBeTruthy();
  expect(requests.find((request) => request.url.split("?")[0].endsWith("/output"))?.init?.body).toBe(JSON.stringify({ node_id: "214" }));
  expect(requests.find((request) => request.url.split("?")[0].endsWith("/output"))?.url).toBe("/api/workflow-profiles/h3/imports/imp-1/output?worker_id=worker-a&worker_url=http%3A%2F%2Fworker-a%3A8188");
  expect(requests.some((request) => request.url === "/api/workflow-profiles/h3/imports/imp-1/analysis?worker_id=worker-a&worker_url=http%3A%2F%2Fworker-a%3A8188")).toBe(true);
});

it("sends the nested confirmed boundary without an agent proposal", async () => {
  selectedOutput = true;
  lifecycleStatus = "mapped";
  localStorage.setItem("director-studio.h3-setup", JSON.stringify({ importId: "imp-1", workerId: "worker-a", workerUrl: workerA.worker_url }));
  render(<H3WorkflowSetup />);
  fireEvent.click(await screen.findByRole("button", { name: "Confirm input nodes" }));
  await waitFor(() => expect(requests.some((request) => request.url.split("?")[0].endsWith("/mapping"))).toBe(true));
  const request = requests.find((item) => item.url.split("?")[0].endsWith("/mapping"));
  expect(JSON.parse(String(request?.init?.body))).toEqual(mapping);
});

it("previews all observed output videos and selects one without another test run", async () => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  jobOutputs = {
    video_candidate_0: { url: "/first.mp4" },
    video_candidate_1: { url: "/second.mp4" },
  };
  localStorage.setItem("director-studio.h3-setup", JSON.stringify({ importId: "imp-1", workerId: "worker-a", workerUrl: workerA.worker_url }));
  render(<H3WorkflowSetup />);
  fireEvent.change(await screen.findByLabelText("Picture for test"), { target: { value: "picture-1" } });
  fireEvent.click(screen.getByRole("button", { name: "Run 56-frame test" }));
  expect(await screen.findByLabelText("Workflow test video 1")).toBeTruthy();
  expect(screen.getByLabelText("Workflow test video 2")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Use video 2" }));
  await waitFor(() => expect(requests.some((request) => request.url.split("?")[0].endsWith("/test-output"))).toBe(true));
  expect(requests.filter((request) => request.url.split("?")[0].endsWith("/test"))).toHaveLength(1);
  expect(JSON.parse(String(requests.find((request) => request.url.endsWith("/test"))?.init?.body))).toEqual({ picture_asset_id: "picture-1", audio_asset_id: null, ...workerA });
  expect(requests.find((request) => request.url.split("?")[0].endsWith("/test-output"))?.init?.body).toBe(JSON.stringify({ artifact_index: 1 }));
});

it("requires explicit worker choice even when only one worker is configured", async () => {
  render(<H3WorkflowSetup />);
  await screen.findByRole("option", { name: "worker-a — up" });
  expect((screen.getByLabelText("Worker for discovery and test") as HTMLSelectElement).value).toBe("");
  expect((screen.getByLabelText("Import Workflow") as HTMLInputElement).disabled).toBe(true);
  expect(requests.some((request) => request.url.includes("/analysis"))).toBe(false);
});

it("validates against the explicitly remembered worker", async () => {
  selectedOutput = true;
  lifecycleStatus = "mapped";
  localStorage.setItem("director-studio.h3-setup", JSON.stringify({ importId: "imp-1", workerId: "worker-a", workerUrl: workerA.worker_url }));
  render(<H3WorkflowSetup />);
  const validate = await screen.findByRole("button", { name: "Validate with ComfyUI" });
  await waitFor(() => expect((validate as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(validate);
  await waitFor(() => expect(requests.some((request) => request.url === "/api/workflow-profiles/h3/imports/imp-1/validate?worker_id=worker-a&worker_url=http%3A%2F%2Fworker-a%3A8188")).toBe(true));
});

function rememberImport() {
  localStorage.setItem("director-studio.h3-setup", JSON.stringify({ importId: "imp-1", workerId: workerA.worker_id, workerUrl: workerA.worker_url }));
}

function activationButton() {
  return screen.getAllByRole("button", { name: "Use Workflow" }).at(-1) as HTMLButtonElement;
}

it("persists a worker change and does not restore the previous worker's tested evidence on reload", async () => {
  selectedOutput = true;
  lifecycleStatus = "tested";
  rememberImport();
  const view = render(<H3WorkflowSetup />);
  await waitFor(() => expect(activationButton().disabled).toBe(false));
  fireEvent.change(screen.getByLabelText("Worker for discovery and test"), { target: { value: "worker-b" } });
  await screen.findByText("Worker binding changed; validate and test again.");
  expect(binding).toEqual(workerB);
  expect(activationButton().disabled).toBe(true);
  expect(JSON.parse(String(requests.find((item) => item.url.endsWith("/worker"))?.init?.body))).toEqual(workerB);
  expect(JSON.parse(localStorage.getItem("director-studio.h3-setup") || "{}")).toEqual({ importId: "imp-1", workerId: "worker-b", workerUrl: workerB.worker_url });
  view.unmount();
  requests = [];
  render(<H3WorkflowSetup />);
  await screen.findByText("Inputs confirmed");
  expect(activationButton().disabled).toBe(true);
  expect(requests.some((item) => item.url.includes("/jobs/job-1"))).toBe(false);
  expect(requests.find((item) => item.url.includes("/analysis"))?.url).toContain("worker_id=worker-b&worker_url=http%3A%2F%2Fworker-b%3A8188");
});

it("clears the server binding and invalidates tested evidence when worker selection is cleared", async () => {
  selectedOutput = true;
  lifecycleStatus = "tested";
  rememberImport();
  render(<H3WorkflowSetup />);
  await waitFor(() => expect(activationButton().disabled).toBe(false));
  fireEvent.change(screen.getByLabelText("Worker for discovery and test"), { target: { value: "" } });
  await waitFor(() => expect(binding).toBeNull());
  expect(activationButton().disabled).toBe(true);
  expect(JSON.parse(String(requests.find((item) => item.url.endsWith("/worker"))?.init?.body))).toEqual({ worker_id: null, worker_url: null });
  expect(lifecycleStatus).toBe("mapped");
});

it.each(["down", "removed", "changed URL"])("cannot activate tested evidence when its worker is %s", async (condition) => {
  selectedOutput = true;
  lifecycleStatus = "tested";
  rememberImport();
  if (condition === "down") fleet[0].status = "down";
  if (condition === "removed") fleet = fleet.slice(1);
  if (condition === "changed URL") fleet[0].base_url = "http://replacement:8188";
  render(<H3WorkflowSetup />);
  await screen.findByLabelText("Final video node");
  expect(activationButton().disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Run 56-frame test" }) as HTMLButtonElement).disabled).toBe(true);
  expect(requests.some((item) => item.url.includes("/activate"))).toBe(false);
  if (condition === "changed URL") {
    expect(screen.getByRole("alert").textContent).toContain("Existing validation and test evidence cannot authorize this endpoint");
    expect(requests.find((item) => item.url.includes("/analysis"))?.url).toContain("worker_url=http%3A%2F%2Fworker-a%3A8188");
    fireEvent.click(screen.getByRole("button", { name: "Bind configured endpoint" }));
    await waitFor(() => expect(binding?.worker_url).toBe("http://replacement:8188"));
    await screen.findByText("Inputs confirmed");
    expect(activationButton().disabled).toBe(true);
  }
});

it("ignores a completed old test poll after switching workers", async () => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  rememberImport();
  let finishJob: ((response: Response) => void) | undefined;
  const fetchOriginal = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => {
    if (String(input).includes("/jobs/job-1")) return new Promise<Response>((resolve) => { finishJob = resolve; });
    return fetchOriginal(input, init);
  });
  render(<H3WorkflowSetup />);
  await screen.findByLabelText("Final video node");
  fireEvent.change(screen.getByLabelText("Picture for test"), { target: { value: "picture-1" } });
  fireEvent.click(screen.getByRole("button", { name: "Run 56-frame test" }));
  await waitFor(() => expect(finishJob).toBeDefined());
  fireEvent.change(screen.getByLabelText("Worker for discovery and test"), { target: { value: "worker-b" } });
  await screen.findByText("Inputs confirmed");
  await act(async () => {
    finishJob!(new Response(JSON.stringify({ id: "job-1", status: "succeeded", outputs: { video_candidate_0: { url: "/stale.mp4" } }, error: null, ...workerA })));
  });
  expect(activationButton().disabled).toBe(true);
  expect(screen.queryByLabelText("Workflow test video 1")).toBeNull();
  expect(screen.getByText("Inputs confirmed")).toBeTruthy();
  expect(binding).toEqual(workerB);
});

it("loads the import editor despite broken active selection and recovers only after explicit built-in selection", async () => {
  selectedOutput = true;
  lifecycleStatus = "mapped";
  rememberImport();
  let recovered = false;
  const fetchOriginal = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = String(input);
    if (url.endsWith("/select")) recovered = true;
    if (url === "/api/workflow-profiles/h3") return Promise.resolve(new Response(JSON.stringify(recovered
      ? { active: { ...active, selection_source: "explicit" }, profiles: [{ ...active, status: "active" }], selected_profile_id: active.profile_id, active_error: null }
      : { active: null, profiles: [{ ...active, status: "available" }], selected_profile_id: "missing-custom", active_error: { code: "profile_missing", message: "Custom workflow file is missing" } })));
    return fetchOriginal(input, init);
  });
  render(<H3WorkflowSetup />);
  expect(await screen.findByLabelText("Final video node")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("missing-custom: Custom workflow file is missing");
  expect(recovered).toBe(false);
  fireEvent.change(screen.getByLabelText("Installed workflow"), { target: { value: active.profile_id } });
  fireEvent.click(screen.getAllByRole("button", { name: "Use Workflow" })[0]);
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(recovered).toBe(true);
  expect(requests.filter((item) => item.url.endsWith("/select"))).toHaveLength(1);
});


it("restores a current multi-video test from server lifecycle after reload", async () => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  pendingTestId = "job-1";
  jobOutputs = { video_candidate_0: { url: "/first.mp4" }, video_candidate_1: { url: "/second.mp4" } };
  rememberImport();
  render(<H3WorkflowSetup />);
  expect(await screen.findByLabelText("Workflow test video 2")).toBeTruthy();
  expect(activationButton().disabled).toBe(true);
  expect(requests.some((item) => item.url.endsWith("/test"))).toBe(false);
});

it("requires confirmation of locally changed input nodes before validating", async () => {
  selectedOutput = true;
  lifecycleStatus = "mapped";
  rememberImport();
  render(<H3WorkflowSetup />);
  await screen.findByLabelText("Seed node (optional)");
  fireEvent.change(screen.getByLabelText("Seed node (optional)"), { target: { value: "" } });
  expect((screen.getByRole("button", { name: "Validate with ComfyUI" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("Confirm changed input nodes before validating.")).toBeTruthy();
});


it.each(["queued", "uploading"])("keeps polling an unbound %s test until the pinned job reports its memory failure", async (initialStatus) => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  pendingTestId = "job-1";
  rememberImport();
  let jobPolls = 0;
  const memoryError = "worker-a: free RAM 10.9 GiB (required 64.0 GiB), free VRAM 4.8 GiB (required 64.0 GiB)";
  const fetchOriginal = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => {
    if (String(input).includes("/jobs/job-1")) {
      jobPolls += 1;
      return Promise.resolve(new Response(JSON.stringify(jobPolls === 1
        ? { id: "job-1", status: initialStatus, worker_id: null, worker_url: null, outputs: {}, error: null }
        : { id: "job-1", status: "failed", ...workerA, outputs: {}, error: memoryError })));
    }
    return fetchOriginal(input, init);
  });
  render(<H3WorkflowSetup />);
  await waitFor(() => expect(jobPolls).toBe(1));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(activationButton().disabled).toBe(true);
  expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveProperty("textContent", memoryError);
  expect(jobPolls).toBe(2);
  expect(screen.getByText("worker-a")).toBeTruthy();
  expect(activationButton().disabled).toBe(true);
});

it.each(["failed", "cancelled"])("shows the preparation error for a test that %s before worker assignment", async (status) => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  pendingTestId = "job-1";
  rememberImport();
  const preparationError = "No eligible render worker has matching validation and test evidence";
  const fetchOriginal = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => String(input).includes("/jobs/job-1")
    ? Promise.resolve(new Response(JSON.stringify({ id: "job-1", status, worker_id: null, worker_url: null, outputs: {}, error: preparationError })))
    : fetchOriginal(input, init));
  render(<H3WorkflowSetup />);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", preparationError);
  expect(activationButton().disabled).toBe(true);
  expect(screen.queryByText(/different worker assignment/)).toBeNull();
});

it.each([
  { status: "running", ...workerB },
  { status: "failed", ...workerB },
  { status: "queued", worker_id: workerA.worker_id, worker_url: null },
  { status: "uploading", worker_id: null, worker_url: workerA.worker_url },
  { status: "running", worker_id: null, worker_url: null },
  { status: "succeeded", worker_id: null, worker_url: null },
])("rejects a mismatched or incomplete worker pin in $status", async (jobIdentity) => {
  selectedOutput = true;
  lifecycleStatus = "validated";
  pendingTestId = "job-1";
  rememberImport();
  const fetchOriginal = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => String(input).includes("/jobs/job-1")
    ? Promise.resolve(new Response(JSON.stringify({ id: "job-1", ...jobIdentity, outputs: { video_candidate_0: { url: "/untrusted.mp4" } }, error: null })))
    : fetchOriginal(input, init));
  render(<H3WorkflowSetup />);
  expect((await screen.findByRole("alert")).textContent).toContain("missing, incomplete, or different worker assignment");
  expect(activationButton().disabled).toBe(true);
  expect(screen.queryByLabelText("Workflow test video 1")).toBeNull();
});
