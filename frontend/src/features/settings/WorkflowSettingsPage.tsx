import { PageShell } from "../../shared/components/PageShell";
import { RenderWorkerSettings } from "../../shared/components/RenderWorkers";
import { H3WorkflowSetup } from "./H3WorkflowSetup";

export function WorkflowSettingsPage({ active = true }: { active?: boolean }) {
  return (
    <PageShell
      title="Settings"
      subtitle="Workflows / H3"
      className="workflow-settings-page"
    >
      <RenderWorkerSettings active={active} />
      <H3WorkflowSetup active={active} />
    </PageShell>
  );
}
