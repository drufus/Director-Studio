import type { SharedRenderJobMetadata } from "../api/types";

function gib(bytes: number | null): string {
  return bytes === null ? "Unknown" : `${(bytes / (1024 ** 3)).toFixed(1)} GiB`;
}

export function RenderJobInfo({ job }: { job: SharedRenderJobMetadata | null }) {
  if (!job?.worker_id) return null;
  return <div className="render-job-info">
    <span>Worker: <strong>{job.worker_id}</strong></span>
    {job.worker_url ? <code>{job.worker_url}</code> : null}
    {job.worker_selected_at ? <small>Assigned {new Date(job.worker_selected_at).toLocaleString()}</small> : null}
    {job.memory_admission ? <details className="render-job-memory">
      <summary>Memory at submission: {job.memory_admission.accepted ? "accepted" : "rejected"}</summary>
      <p>RAM free: {gib(job.memory_admission.ram_free_bytes)} / {gib(job.memory_admission.ram_total_bytes)}; required free: {gib(job.memory_admission.min_free_ram_bytes)}.</p>
      {job.memory_admission.devices.map((device) => <p key={device.index}>{device.name}: VRAM free {gib(device.vram_free_bytes)} / {gib(device.vram_total_bytes)}; required free: {gib(job.memory_admission!.min_free_vram_bytes)}.</p>)}
      <small>Checked {new Date(job.memory_admission.checked_at).toLocaleString()}</small>
      {job.memory_admission.error ? <p className="banner error">{job.memory_admission.error}</p> : null}
    </details> : null}
  </div>;
}
