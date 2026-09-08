import { useEffect, useState } from "react";
import { fetchRenderWorkers } from "../api/client";
import type { RenderWorkerStatus } from "../api/types";

export function useRenderWorkers(active = true) {
  const [workers, setWorkers] = useState<RenderWorkerStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await fetchRenderWorkers();
        if (!cancelled) {
          setWorkers(result.workers);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) timer = setTimeout(refresh, 10000);
      }
    };
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [active]);
  return { workers, error };
}

export function RenderWorkerHealth() {
  const { workers, error } = useRenderWorkers();
  const up = workers?.filter((worker) => worker.status === "up").length ?? 0;
  const count = workers?.length ?? 0;
  const healthy = !error && count > 0 && up === count;
  const label = error ? "Render worker status unavailable" : !workers
    ? "Checking render workers" : `Render workers: ${up}/${count} up`;
  const details = error || workers?.map((worker) => `${worker.id}: ${worker.status}${worker.error ? ` — ${worker.error}` : ""}`).join("\n") || label;
  return <details className={`worker-health health ${healthy ? "ok" : "bad"}`}>
    <summary aria-label={label} title={details}><span className="dot" /><span>{label}</span></summary>
    <div className="worker-health-details" role="status">
      {error ? <p>{error}</p> : !workers ? <p>{label}</p> : workers.length === 0 ? <p>No render workers configured.</p> : workers.map((worker) => (
        <p key={worker.id}><strong>{worker.id}: {worker.status}</strong>{worker.error ? <span> — {worker.error}</span> : null}</p>
      ))}
    </div>
  </details>;
}

export function RenderWorkerSettings({ active = true }: { active?: boolean }) {
  const { workers, error } = useRenderWorkers(active);
  return <section className="section-card render-worker-settings" aria-labelledby="render-workers-title">
    <h2 id="render-workers-title" className="section-card-title">Render workers</h2>
    <p className="field-hint">Configured on the server. Each submitted job stays assigned to its worker through output retrieval.</p>
    {error ? <div className="banner error" role="alert">Worker status unavailable: {error}</div> : null}
    {!workers ? <p>Loading render workers…</p> : workers.length === 0 ? <p>No render workers configured.</p> : <div className="render-worker-table-scroll"><table className="render-worker-table">
      <thead><tr><th>Worker</th><th>Endpoint</th><th>Status</th><th>Running</th><th>Queued</th><th>Last checked</th></tr></thead>
      <tbody>{workers.map((worker) => <tr key={worker.id}>
        <th scope="row">{worker.id}</th><td><code>{worker.base_url}</code></td>
        <td><strong>{error ? "Status stale" : worker.status}</strong>{worker.error ? <p className="error-copy">{worker.error}</p> : null}</td>
        <td>{worker.running_jobs ?? "Unknown"}</td><td>{worker.queued_jobs ?? "Unknown"}</td>
        <td>{worker.checked_at ? <time dateTime={worker.checked_at}>{new Date(worker.checked_at).toLocaleString()}</time> : "Not checked"}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>;
}
