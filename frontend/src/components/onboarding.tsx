import { useEffect, useState } from "react";
import { Check, CircleAlert, RefreshCw } from "lucide-react";
import { AgentHandoff } from "@/components/agent-handoff";
import { OpenRouterSettings } from "@/components/inference-settings";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, errorMessage, type EmployeeAccess, type EmployeeChannel } from "@/lib/api";
import { onboardingProgress, type HandoffReceipt, type OnboardingAgent } from "@/lib/onboarding";

type Snapshot = {
  agents: OnboardingAgent[];
  configured: boolean;
  channels: EmployeeChannel[];
  accesses: EmployeeAccess[];
  handoffs: Record<string, HandoffReceipt[]>;
};

export function Onboarding({ query }: { query: string }) {
  const selectedId = new URLSearchParams(query).get("agent_id");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const options = { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]) };
        const [allAgents, provider, channels, accesses] = await Promise.all([
          api<OnboardingAgent[]>("/agents", options),
          api<{ configured: boolean }>("/inference/provider", options),
          api<EmployeeChannel[]>("/channels", options),
          api<EmployeeAccess[]>("/employee-accesses", options),
        ]);
        const agents = allAgents.filter((item) => item.runtime_mode === "native");
        const agent = selectedId ? agents.find((item) => item.id === selectedId) : agents[0];
        const handoffs = await Promise.all(accesses.filter((access) =>
          access.agent_id === agent?.id && access.employee_id === agent.employee_id,
        ).map(async (access) => [access.id, (await api<{ history: HandoffReceipt[] }>(
          `/employee-accesses/${access.id}/handoff`, options,
        )).history] as const));
        if (!controller.signal.aborted) {
          setSnapshot({ agents, configured: provider.configured, channels, accesses, handoffs: Object.fromEntries(handoffs) });
          setError(null);
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          setSnapshot(null);
          setError(errorMessage(cause));
        }
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void load(), 5000);
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [selectedId, refresh]);

  const agent = selectedId ? snapshot?.agents.find((item) => item.id === selectedId) : snapshot?.agents[0];
  const progress = snapshot && onboardingProgress(agent, snapshot.configured, snapshot.channels, snapshot.accesses, snapshot.handoffs);
  const complete = progress && Object.values(progress).every(Boolean);
  const settingsLink = agent ? `#agents/${agent.id}/settings` : "#agents";
  const steps = [
    {
      key: "model" as const,
      title: "Connect a model",
      detail: agent?.inference_override?.model_id
        ? `${agent.inference_override.model_id} · ${snapshot?.configured ? "OpenRouter key configured" : "OpenRouter key needed"}.`
        : progress?.model
          ? "The native provider responded to the employee’s verified conversation."
          : "Save an OpenRouter key below, then choose OpenRouter and a model when creating your agent. Advanced native-provider setup remains available in agent settings.",
      href: settingsLink, action: "Choose model",
    },
    {
      key: "employee" as const,
      title: "Assign an employee and permissions",
      detail: agent?.employee_id
        ? `${agent.employee_name} · ${agent.role?.name ?? "No role"}. ${progress?.employee ? "Permissions applied." : "Review and apply pending permissions in agent settings."}`
        : "Create a role with the tools this employee needs, then create the employee and assign that role.",
      href: "#employees", action: "Manage employees",
    },
    {
      key: "runtime" as const,
      title: "Start the employee’s agent",
      detail: agent
        ? `${agent.runtime_release} · ${agent.observed_state.replaceAll("_", " ")}. Change assignment or start the agent from its settings.`
        : "Use New agent in the sidebar. Choose the employee, OpenClaw or Hermes, and the supported version; Latest supported is the default.",
      href: settingsLink, action: "Agent settings",
    },
    {
      key: "identity" as const,
      title: "Connect Telegram or Slack and approve the employee",
      detail: "In Access & availability below, save channel credentials, check and enable the channel, then review and approve the employee’s external identity.",
    },
    {
      key: "delivery" as const,
      title: "Verify the first employee conversation",
      detail: progress?.delivery
        ? "The channel provider accepted every part of an agent reply after the employee’s verification message. This confirms delivery, not that the message was read."
        : "Create a delivery test below and share its instructions. The employee sends the verification message, then a normal message. Completion requires the full agent reply to be accepted by the channel provider.",
    },
  ];

  return (
    <section aria-labelledby="onboarding-heading" className="mx-auto max-w-3xl space-y-8">
      <div className="page-heading">
        <div>
          <h1 id="onboarding-heading">Set up your first employee</h1>
          <p>Finish with a working conversation in Telegram or Slack. Saved settings determine your progress when you return.</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => setRefresh((value) => value + 1)}><RefreshCw />Refresh</Button>
      </div>
      {error && <Alert variant="destructive"><CircleAlert /><AlertDescription>{error} Progress could not be checked. Refresh to try again.</AlertDescription></Alert>}
      {!snapshot && !error && <Skeleton className="h-48" />}
      {snapshot && <>
        <div className="space-y-3">
          {snapshot.agents.length > 0 ? <>
            <Label htmlFor="onboarding-agent">Employee agent</Label>
            <Select value={agent?.id ?? ""} onValueChange={(id) => { window.location.hash = `onboarding?agent_id=${encodeURIComponent(id)}`; }}>
              <SelectTrigger id="onboarding-agent" className="w-full"><SelectValue placeholder="Choose a saved agent" /></SelectTrigger>
              <SelectContent>{snapshot.agents.map((item) => <SelectItem key={item.id} value={item.id}>{item.display_name} · {item.employee_name || "Unassigned"}</SelectItem>)}</SelectContent>
            </Select>
          </> : <p className="text-sm text-muted-foreground">No native agents yet. Start with a provider and employee, then use New agent in the sidebar.</p>}
          {selectedId && !agent && <p role="status" className="text-sm text-destructive">This agent is no longer available. Select another saved agent to continue.</p>}
          <p role="status" className="text-sm">{complete ? "First employee setup verified." : `${progress ? Object.values(progress).filter(Boolean).length : 0} of 5 steps complete.`}</p>
        </div>
        <ol className="divide-y border-y">
          {steps.map((step, index) => <li key={step.key} className="space-y-2 py-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-medium">{index + 1}. {step.title}</h2>
              <Badge variant={progress?.[step.key] ? "success" : "secondary"}>{progress?.[step.key] ? <><Check className="size-3" />Complete</> : "Incomplete"}</Badge>
            </div>
            <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{step.detail}</p>
            {step.href && <a href={step.href} className="inline-block text-sm text-primary underline underline-offset-4">{step.action}</a>}
            {step.key === "employee" && <a href="#roles" className="ml-4 inline-block text-sm text-primary underline underline-offset-4">Manage roles</a>}
          </li>)}
        </ol>
      </>}
      <details className="border-b pb-6" open={!snapshot?.configured}>
        <summary className="mb-5 cursor-pointer font-medium">Configure OpenRouter</summary>
        <OpenRouterSettings />
      </details>
      {agent && <AgentHandoff key={agent.id} agent={agent} disabled={!!error} />}
    </section>
  );
}
