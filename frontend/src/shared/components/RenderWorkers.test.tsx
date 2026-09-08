// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { fetchRenderWorkers } from "../api/client";
import type { RenderWorkerStatus } from "../api/types";
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
    min_free_ram_bytes: 8 * 1024 ** 3, min_free_vram_bytes: 12 * 1024 ** 3,
    accepted: false, error: "render-a: insufficient free VRAM: 4.0 GiB; required 12.0 GiB",
  } }} />);
  expect(screen.getByText("render-a")).toBeTruthy();
  expect(screen.getByText("Memory at submission: rejected")).toBeTruthy();
  expect(screen.getByText(/RAM free: 5.0 GiB/)).toBeTruthy();
  expect(screen.getByText(/GPU 0: VRAM free 4.0 GiB/)).toBeTruthy();
  expect(screen.getByText("render-a: insufficient free VRAM: 4.0 GiB; required 12.0 GiB")).toBeTruthy();
});
