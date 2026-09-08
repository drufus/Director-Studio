import type { SharedRenderJobMetadata } from "../api/types";

function gib(bytes: number | null): string {
  return bytes === null ? "Unknown" : `${(bytes / (1024 ** 3)).toFixed(1)} GiB`;
}

const memoryStatus = {
  recording: "Recording",
  completed: "Complete",
  incomplete: "Incomplete",
  interrupted: "Interrupted",
};

export function RenderJobInfo({ job }: { job: SharedRenderJobMetadata | null }) {
  if (!job?.worker_id) return null;
  return <div className="render-job-info">
    <span>Worker: <strong>{job.worker_id}</strong></span>
    {job.worker_url ? <code>{job.worker_url}</code> : null}
    {job.worker_selected_at ? <small>Assigned {new Date(job.worker_selected_at).toLocaleString()}</small> : null}
    {job.memory_admission ? <details className="render-job-memory">
      <summary>Memory at submission: {job.memory_admission.accepted ? "accepted" : "rejected"}</summary>
      {job.memory_admission.threshold_provisional ? <p className="banner">Provisional VRAM threshold for calibration; the production threshold has not been set.</p> : null}
      <p>RAM free: {gib(job.memory_admission.ram_free_bytes)} / {gib(job.memory_admission.ram_total_bytes)} (informational).</p>
      {job.memory_admission.devices.map((device) => <p key={device.index}>{device.name}: VRAM free {gib(device.vram_free_bytes)} / {gib(device.vram_total_bytes)}; required free: {gib(job.memory_admission!.min_free_vram_bytes)}.</p>)}
      <small>Checked {new Date(job.memory_admission.checked_at).toLocaleString()}</small>
      {job.memory_admission.error ? <p className="banner error">{job.memory_admission.error}</p> : null}
    </details> : null}
    {job.memory_usage ? <details className="render-job-memory">
      <summary>VRAM during execution: {memoryStatus[job.memory_usage.status]}</summary>
      <p>Sampled across the whole worker pool, including other workloads. This is not memory allocated exclusively to this job.</p>
      {job.memory_usage.devices.map((device) => <div key={device.index}>
        <p><strong>{device.name}</strong>: total VRAM {gib(device.vram_total_bytes)}.</p>
        <p>Free VRAM at submission: {gib(device.baseline_vram_free_bytes)}; minimum sampled free: {gib(device.min_vram_free_bytes)}.</p>
        <p>Sampled peak used VRAM: {gib(device.peak_vram_used_bytes)}; increase from submission: {gib(device.peak_vram_delta_bytes)}.</p>
        {device.peak_at ? <small>Peak sampled {new Date(device.peak_at).toLocaleString()}</small> : null}
      </div>)}
      <p>{job.memory_usage.sample_count} samples; interval {job.memory_usage.sample_interval_sec} seconds. Shorter spikes may be missed.</p>
      <small>Started {new Date(job.memory_usage.started_at).toLocaleString()}{job.memory_usage.finished_at ? `; finished ${new Date(job.memory_usage.finished_at).toLocaleString()}` : ""}</small>
      {job.memory_usage.errors.length ? <div role="alert" className="banner error">
        <p>Memory telemetry has gaps; the recorded peak may be incomplete.</p>
        {job.memory_usage.errors.map((error, index) => <p key={`${error.sampled_at}-${index}`}>{new Date(error.sampled_at).toLocaleString()} ({error.phase}): {error.error}</p>)}
      </div> : null}
    </details> : null}
  </div>;
}
