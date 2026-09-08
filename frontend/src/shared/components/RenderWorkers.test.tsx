// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { fetchRenderWorkers } from "../api/client";
import type { RenderMemoryUsage, RenderWorkerStatus } from "../api/types";
import { RenderWorkerHealth, RenderWorkerSettings } from "./RenderWorkers";
import { RenderJobInfo } from "./RenderJobInfo";

vi.mock("../api/client", () => ({ fetchRenderWorkers: vi.fn() }));

const worker = (id: string, overrides: Partial<RenderWorkerStatus> = {}): RenderWorkerStatus => ({
  id, base_url: `http://${id}:8188`, status: "up", checked_at: null, error: null,
  running_jobs: 1, queued_jobs: 0, ...overrides,
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); });

it("keeps a failed worker visible when another worker is healthy", async () => {
  vi.mocked(fetchRenderWorkers).mockResolvedValue({ workers: [
    worker("render-a"), worker("render-b", { status: "down", error: "render-b connection refused", running_jobs: null, queued_jobs: null }),
  ] });
  render(<RenderWorkerHealth />);
  const summary = await screen.findByLabelText("Render workers: 1/2 up");
  expect(summary.parentElement?.classList.contains("bad")).toBe(true);
  expect(summary.getAttribute("title")).toContain("render-b connection refused");
});

it("refreshes an initially healthy fleet and exposes a worker failing mid-session", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchRenderWorkers).mockResolvedValueOnce({ workers: [worker("render-a"), worker("render-b")] })
    .mockResolvedValueOnce({ workers: [worker("render-a"), worker("render-b", { status: "down", error: "Request timed out" })] });
  render(<RenderWorkerHealth />);
  await act(async () => {});
  expect(screen.getByLabelText("Render workers: 2/2 up").parentElement?.classList.contains("ok")).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.getByLabelText("Render workers: 1/2 up").getAttribute("title")).toContain("Request timed out");
});

it("shows unknown queue counts and the exact worker error in Settings", async () => {
  vi.mocked(fetchRenderWorkers).mockResolvedValue({ workers: [worker("render-a", {
    status: "down", error: "GET /queue: HTTP 503", running_jobs: null, queued_jobs: null,
  })] });
  render(<RenderWorkerSettings />);
  expect(await screen.findByText("GET /queue: HTTP 503")).toBeTruthy();
  expect(screen.getAllByText("Unknown")).toHaveLength(2);
  expect(screen.getByText("http://render-a:8188")).toBeTruthy();
});

it("marks old worker measurements stale when refreshing status fails", async () => {
  vi.mocked(fetchRenderWorkers).mockRejectedValue(new Error("Worker registry unavailable"));
  render(<RenderWorkerSettings />);
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Worker registry unavailable"));
});

it("shows the assigned worker and the measured reason memory admission failed", () => {
  render(<RenderJobInfo job={{ worker_id: "render-a", worker_url: "http://render-a:8188", memory_admission: {
    checked_at: "2026-09-08T01:00:00Z", worker_id: "render-a", worker_url: "http://render-a:8188",
    ram_free_bytes: 5 * 1024 ** 3, ram_total_bytes: 121 * 1024 ** 3,
    devices: [{ index: 0, name: "GPU 0", vram_free_bytes: 4 * 1024 ** 3, vram_total_bytes: 121 * 1024 ** 3 }],
    min_free_vram_bytes: 12 * 1024 ** 3, metric: "vram_free",
    accepted: false, error: "render-a: insufficient free VRAM: 4.0 GiB; required 12.0 GiB",
  } }} />);
  expect(screen.getByText("render-a")).toBeTruthy();
  expect(screen.getByText("Memory at submission: rejected")).toBeTruthy();
  expect(screen.getByText(/RAM free: 5.0 GiB/)).toBeTruthy();
  expect(screen.getByText(/RAM free:.*informational/).textContent).not.toContain("required");
  expect(screen.getByText(/GPU 0: VRAM free 4.0 GiB/)).toBeTruthy();
  expect(screen.getByText("render-a: insufficient free VRAM: 4.0 GiB; required 12.0 GiB")).toBeTruthy();
});

const memoryUsage = (overrides: Partial<RenderMemoryUsage> = {}): RenderMemoryUsage => ({
  status: "completed", worker_id: "render-a", worker_url: "http://render-a:8188",
  sample_interval_sec: 1, sample_count: 60,
  started_at: "2026-09-08T08:00:00Z", finished_at: "2026-09-08T08:01:00Z",
  devices: [{
    index: 0, name: "GB10", vram_total_bytes: 121 * 1024 ** 3,
    baseline_vram_free_bytes: 32.6 * 1024 ** 3, min_vram_free_bytes: 12.5 * 1024 ** 3,
    peak_vram_used_bytes: 108.5 * 1024 ** 3, peak_vram_delta_bytes: 20.1 * 1024 ** 3,
    peak_at: "2026-09-08T08:00:40Z",
  }],
  samples: [], errors: [], note: "Whole worker pool measurements.", ...overrides,
});

it("shows the measured worker-pool peak, baseline change, and sampling limits", () => {
  render(<RenderJobInfo job={{ worker_id: "render-a", memory_usage: memoryUsage() }} />);
  expect(screen.getByText("VRAM during execution: Complete")).toBeTruthy();
  expect(screen.getByText(/total VRAM 121.0 GiB/)).toBeTruthy();
  expect(screen.getByText("Free VRAM at submission: 32.6 GiB; minimum sampled free: 12.5 GiB.")).toBeTruthy();
  expect(screen.getByText("Sampled peak used VRAM: 108.5 GiB; increase from submission: 20.1 GiB.")).toBeTruthy();
  expect(screen.getByText(`Peak sampled ${new Date("2026-09-08T08:00:40Z").toLocaleString()}`)).toBeTruthy();
  expect(screen.getByText(/60 samples; interval 1 seconds. Shorter spikes may be missed/)).toBeTruthy();
  expect(screen.getByText(/whole worker pool, including other workloads/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("marks the threshold provisional and exposes telemetry gaps beside the measured peak", () => {
  render(<RenderJobInfo job={{ worker_id: "render-a", memory_admission: {
    checked_at: "2026-09-08T08:00:00Z", worker_id: "render-a", worker_url: "http://render-a:8188",
    ram_free_bytes: 53 * 1024 ** 3, ram_total_bytes: 121 * 1024 ** 3,
    devices: [{ index: 0, name: "GB10", vram_free_bytes: 32.6 * 1024 ** 3, vram_total_bytes: 121 * 1024 ** 3 }],
    min_free_vram_bytes: 1024 ** 3, metric: "vram_free", threshold_provisional: true,
    accepted: true, error: null,
  }, memory_usage: memoryUsage({ status: "incomplete", errors: [{
    sampled_at: "2026-09-08T08:00:30Z", phase: "execution", error: "render-a: GET /system_stats timed out",
  }] }) }} />);
  expect(screen.getByText(/Provisional VRAM threshold for calibration/)).toBeTruthy();
  expect(screen.getByText("VRAM during execution: Incomplete")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("recorded peak may be incomplete");
  expect(screen.getByRole("alert").textContent).toContain("render-a: GET /system_stats timed out");
  expect(screen.getByText(/Sampled peak used VRAM: 108.5 GiB/)).toBeTruthy();
});

it("keeps device peaks separate and labels unavailable measurements as unknown", () => {
  const usage = memoryUsage();
  usage.devices.push({
    index: 1, name: "GPU 1", vram_total_bytes: null, baseline_vram_free_bytes: null,
    min_vram_free_bytes: null, peak_vram_used_bytes: null, peak_vram_delta_bytes: null, peak_at: null,
  });
  render(<RenderJobInfo job={{ worker_id: "render-a", memory_usage: usage }} />);
  expect(screen.getByText(/GPU 1/)).toBeTruthy();
  expect(screen.getByText("Sampled peak used VRAM: Unknown; increase from submission: Unknown.")).toBeTruthy();
  expect(screen.getByText("Sampled peak used VRAM: 108.5 GiB; increase from submission: 20.1 GiB.")).toBeTruthy();
});
