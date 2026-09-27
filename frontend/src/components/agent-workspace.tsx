import { useEffect, useRef, useState, type FormEvent } from "react";
import { ExternalLink, Bot, CircleAlert, LoaderCircle, Play, Plus, Send, Square, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { UsageDetails, type GenerationSettings, type InferenceSelection, type InferenceCall } from "@/components/generation-settings";
import { InferenceSettings, NativeModelChoice, NativeModelSettings } from "@/components/inference-settings";
import { api, ApiError, errorMessage, type Employee, type Capability, type AgentPermissions } from "@/lib/api";
import { cn } from "@/lib/utils";

type Agent = AgentPermissions & {
  id: string;
  runtime_kind: "openclaw" | "hermes";
  runtime_mode: "native" | "managed";
  display_name: string;
  employee_label: string;
  desired_state: string;
  observed_state: string;
  last_error: string | null;
  model_route: string;
  inference_override: InferenceSelection | null;
};
type Operation = { action?: string; dashboard_url?: string | null; id: string; agent_id: string; status: string; error?: string | null };
type Run = { message: string; inference_calls: InferenceCall[]; inference: { settings?: GenerationSettings }; model_id: string; id: string; agent_id: string; status: string; output?: string | null; error?: string | null; cancel_requested?: boolean };
type RunEvent = { sequence: number; type: string; payload: Record<string, unknown> };
type Mutation = {
  path: string;
  method: "POST" | "DELETE";
  body?: object;
  key: string;
  kind: "create" | "lifecycle" | "diagnostic" | "cancel" | "dashboard";
  agentId?: string;
  popup?: Window | null;
};

const runtimes = { openclaw: "OpenClaw", hermes: "Hermes" } as const;
type RuntimeKind = keyof typeof runtimes;

function RuntimeIcon({ kind, className }: { kind: RuntimeKind; className?: string }) {
  return <img src={`/runtime-icons/${kind}.svg`} alt="" aria-hidden="true" className={cn("shrink-0 object-contain", className)} />;
}

const activeOperations = new Set(["queued", "running", "retry_wait"]);
const activeRuns = new Set(["queued", "dispatching", "running", "cancel_requested", "unknown"]);
const transitionalStates = new Set(["pending", "provisioning", "starting", "stopping", "deleting", "applying"]);
const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

function StateBadge({ state }: { state: string }) {
  const healthy = ["ready", "running", "completed", "succeeded"].includes(state);
  const warning = ["unknown", "failed", "error", "interrupted"].includes(state);
  return <Badge variant={healthy ? "success" : warning ? "warning" : "secondary"} className="capitalize">{state.replaceAll("_", " ")}</Badge>;
}

export function AgentWorkspace({ active = true, operationIds, onOperation }: { active?: boolean; operationIds: Record<string, string>; onOperation: (agentId: string, id: string) => void }) {
  const [agentView, setAgentView] = useState("conversation");
  const [modelId, setModelId] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [operation, setOperation] = useState<Operation | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<{ runId: string; items: RunEvent[] }>({ runId: "", items: [] });
  const cursor = useRef({ runId: "", sequence: 0 });
  const [loading, setLoading] = useState(true);
  const [pollError, setPollError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [retryRequest, setRetryRequest] = useState<Mutation | null>(null);
  const [refresh, setRefresh] = useState(0);
  const createDialog = useRef<HTMLDialogElement>(null);
  const applyDialog = useRef<HTMLDialogElement>(null);
  const [runtimeKind, setRuntimeKind] = useState<RuntimeKind>("openclaw");
  const [createModel, setCreateModel] = useState<string | null>(null);
  const [dashboardPassword, setDashboardPassword] = useState("");
  const [runtimeMode, setRuntimeMode] = useState<"native" | "managed">("native");
  const dashboardWindows = useRef<Record<string, Window>>({});
  const [displayName, setDisplayName] = useState("");
  const [employeeLabel, setEmployeeLabel] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [catalog, setCatalog] = useState<Capability[]>([]);
  const [message, setMessage] = useState("");

  const selected = agents.find((agent) => agent.id === selectedId);
  const runtimeName = runtimes[selected?.runtime_kind ?? "openclaw"];
  const effectiveModel = selected?.inference_override?.model_id ?? modelId;
  const operationId = operationIds[selectedId];
  const selectedRuns = runs.filter((run) => run.agent_id === selectedId);
  const selectedRun = selectedRuns[0];
  const runId = selectedRun?.id;
  const selectedOperation = operation?.id === operationId ? operation : null;
  const operationActive = (!!selectedOperation && activeOperations.has(selectedOperation.status)) || transitionalStates.has(selected?.observed_state ?? "");
  const runActive = !!selectedRun && activeRuns.has(selectedRun.status);
  const busy = operationActive || runActive || agents.some((agent) => transitionalStates.has(agent.observed_state));
  const writesDisabled = submitting || !!retryRequest || !!pollError || loading;
  const runEvents = events.runId === runId ? events.items : [];

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let stopped = false;
    const poll = async () => {
      try {
        const options = { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]) };
        const latestOperation = operationId ? api<Operation>(`/operations/${operationId}`, options).catch((error) => {
          if (error instanceof ApiError && error.status === 404) return null;
          throw error;
        }) : Promise.resolve(null);
        const [nextAgents, nextOperation, nextRuns, nextModel, dashboards, nextEmployees, nextCatalog] = await Promise.all([
          api<Agent[]>("/agents", options), latestOperation,
          selectedId ? api<Run[]>(`/agents/${selectedId}/runs`, options) : Promise.resolve([]),
          api<InferenceSelection>("/inference", options),
          Promise.all(Object.keys(dashboardWindows.current).map((id) => id === operationId ? latestOperation : api<Operation>(`/operations/${id}`, options))),
          api<Employee[]>("/employees", options), api<Capability[]>("/capabilities", options),
        ]);
        const nextRun = nextRuns[0];
        const nextRunId = nextRun?.id;
        const after = cursor.current.runId === nextRunId ? cursor.current.sequence : 0;
        const nextEvents = nextRun ? await api<RunEvent[]>(`/runs/${nextRunId}/events?after=${after}`, options) : [];
        if (stopped) return;
        for (const dashboard of dashboards) {
          if (!dashboard || activeOperations.has(dashboard.status)) continue;
          const popup = dashboardWindows.current[dashboard.id];
          if (popup && !popup.closed) {
            if (dashboard.dashboard_url) popup.location.replace(dashboard.dashboard_url);
            else popup.close();
          }
          if (dashboard.error) setActionError(dashboard.error);
          delete dashboardWindows.current[dashboard.id];
        }
        setAgents(nextAgents); setEmployees(nextEmployees); setCatalog(nextCatalog);
        setModelId(nextModel.model_id);
        setSelectedId((current) => nextAgents.some((agent) => agent.id === current) ? current : nextAgents[0]?.id ?? "");
        setOperation(nextOperation);
        setRuns(nextRuns);
        if (nextRunId) {
          setEvents((current) => ({ runId: nextRunId, items: current.runId === nextRunId ? [...current.items, ...nextEvents] : nextEvents }));
          cursor.current = { runId: nextRunId, sequence: nextEvents.at(-1)?.sequence ?? after };
        }
        setPollError(null);
      } catch (error) {
        if (!stopped) setPollError(`${errorMessage(error)} Displayed records may be out of date.`);
      } finally {
        if (!stopped) {
          setLoading(false);
          timer = window.setTimeout(() => void poll(), busy ? 2000 : 5000);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 0);
    return () => { stopped = true; window.clearTimeout(timer); controller.abort(); };
  }, [selectedId, operationId, runId, busy, refresh, active]);

  async function mutate(request: Mutation) {
    setSubmitting(true);
    setActionError(null);
    try {
      const result = await api<Operation | Run>(request.path, {
        method: request.method,
        headers: { "Idempotency-Key": request.key },
        body: request.body ? JSON.stringify(request.body) : undefined,
        signal: AbortSignal.timeout(15_000),
      });
      if (request.kind === "dashboard" && request.popup && !request.popup.closed) dashboardWindows.current[result.id] = request.popup;
      if (request.kind === "diagnostic") {
        setRuns((current) => [result as Run, ...current.filter((run) => run.id !== result.id)]);
        setMessage("");
      } else if (request.kind !== "cancel") {
        onOperation(result.agent_id, result.id);
        setOperation(result);
        setSelectedId(result.agent_id);
        if (request.kind === "create") { setDisplayName(""); setEmployeeLabel(""); setEmployeeId(""); setDashboardPassword(""); createDialog.current?.close(); }
      }
      setRetryRequest(null);
      setRefresh((value) => value + 1);
    } catch (error) {
      if (request.kind === "dashboard") request.popup?.close();
      const uncertain = !(error instanceof ApiError) || error.status >= 500;
      setRetryRequest(uncertain ? request : null);
      setActionError(uncertain
        ? "The request result is unknown. Retry the same request to recover its result without creating a duplicate."
        : errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  function createAgent(event: FormEvent) {
    event.preventDefault();
    if (!displayName.trim() || (!employeeId && !employeeLabel.trim()) || employeeLabel.trim().length > 160 || writesDisabled) return;
    void mutate({ path: "/agents", method: "POST", key: crypto.randomUUID(), kind: "create", body: {
      display_name: displayName.trim(), ...(employeeId ? { employee_id: employeeId } : { employee_label: employeeLabel.trim() }), runtime_mode: runtimeMode, runtime_kind: runtimeKind,
      ...(runtimeMode === "native" && createModel ? { model_id: createModel } : {}),
      ...(runtimeKind === "hermes" ? { dashboard_password: dashboardPassword } : {}),
    } });
  }

  function lifecycle(action: "start" | "stop" | "delete" | "apply-role", confirmed = false) {
    if (!selected || writesDisabled || operationActive) return;
    if (action === "apply-role" && selected.desired_state === "running" && !confirmed) { applyDialog.current?.showModal(); return; }
    applyDialog.current?.close();
    if (action === "delete" && !window.confirm(`Delete ${selected.display_name}? This removes its runtime and private agent state. This cannot be undone.`)) return;
    void mutate({ path: `/agents/${selected.id}${action === "delete" ? "" : `/${action}`}`, method: action === "delete" ? "DELETE" : "POST", key: crypto.randomUUID(), kind: "lifecycle", agentId: selected.id });
  }

  async function assignEmployee(id: string) {
    if (!selected || !id || writesDisabled) return;
    setSubmitting(true); setActionError(null);
    try {
      const updated = await api<Agent>(`/agents/${selected.id}/employee`, { method: "PUT", body: JSON.stringify({ employee_id: id }) });
      setAgents((current) => current.map((agent) => agent.id === updated.id ? updated : agent));
    } catch (cause) { setActionError(errorMessage(cause)); }
    finally { setSubmitting(false); }
  }

  function openNativeWorkspace() {
    if (!selected || writesDisabled || operationActive) return;
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    void mutate({ path: `/agents/${selected.id}/dashboard`, method: "POST", key: crypto.randomUUID(), kind: "dashboard", agentId: selected.id, popup });
  }

  function sendDiagnostic(event: FormEvent) {
    event.preventDefault();
    if (!selected || !message.trim() || writesDisabled || runActive || operationActive) return;
    void mutate({ path: `/agents/${selected.id}/diagnostic-runs`, method: "POST", key: crypto.randomUUID(), kind: "diagnostic", agentId: selected.id, body: { message: message.trim() } });
  }

  return (
    <section aria-label="Agent workspace" className="agent-workspace">
      <div className="page-heading">
        <div><h1>Agents</h1><p>Manage your agents and their runtime activity.</p></div>
        <Button disabled={writesDisabled} onClick={() => { setActionError(null); createDialog.current?.showModal(); }}><Plus aria-hidden="true" />New agent</Button>
      </div>
      {(actionError || pollError) && <div role="alert" className="notice notice-warning mb-5 flex-wrap">
        <CircleAlert className="size-4 shrink-0" aria-hidden="true" />
        <p className="min-w-0 flex-1">{actionError ?? pollError}</p>
        {retryRequest && <Button variant="outline" size="sm" disabled={submitting} onClick={() => void mutate(retryRequest)}>Retry same request</Button>}
      </div>}

      <dialog ref={applyDialog} aria-labelledby="apply-role-title" className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-xl bg-panel p-6 text-foreground shadow-xl backdrop:bg-black/65">
        <h2 id="apply-role-title" className="text-lg font-semibold">Apply saved role?</h2><p className="my-4 text-sm text-muted-foreground">Running work will be interrupted. This agent will restart with the captured permissions.</p><div className="flex justify-end gap-3"><Button variant="outline" onClick={() => applyDialog.current?.close()}>Cancel</Button><Button onClick={() => lifecycle("apply-role", true)}>Apply and restart</Button></div>
      </dialog>
      <dialog ref={createDialog} aria-labelledby="create-agent-title" className="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-xl bg-panel p-6 text-foreground shadow-xl backdrop:bg-black/65" onCancel={(event) => { if (submitting) event.preventDefault(); }}>
        <form onSubmit={createAgent} className="space-y-5">
          <h2 id="create-agent-title" className="text-lg font-semibold tracking-tight">Create an agent</h2>
          <div><label htmlFor="agent-name" className="mb-1.5 block text-xs text-muted-foreground">Name</label><input id="agent-name" autoFocus className={inputClass} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Sales assistant" maxLength={120} required disabled={writesDisabled} /></div>
          <div><label htmlFor="create-employee" className="mb-1.5 block text-xs text-muted-foreground">Employee</label><select id="create-employee" className={inputClass} value={employeeId} onChange={(event) => setEmployeeId(event.target.value)} disabled={writesDisabled}><option value="">Unassigned — use a legacy label</option>{employees.map((employee) => <option key={employee.id} value={employee.id}>{employee.name}</option>)}</select><a href="#employees" onClick={() => createDialog.current?.close()} className="mt-2 block text-xs text-primary underline">Manage employees</a></div>
          {!employeeId && <div><label htmlFor="employee-label" className="mb-1.5 block text-xs text-muted-foreground">Employee label</label><input id="employee-label" className={inputClass} value={employeeLabel} onChange={(event) => setEmployeeLabel(event.target.value)} placeholder="Alex" required minLength={1} maxLength={160} disabled={writesDisabled} /></div>}
          <fieldset disabled={writesDisabled}>
            <legend className="mb-2 text-sm font-medium">Agent runtime</legend>
            <div className="grid grid-cols-2 gap-3">
              {(Object.entries(runtimes) as [RuntimeKind, string][]).map(([kind, name]) => <label key={kind} className="relative cursor-pointer">
                <input type="radio" name="runtime-kind" value={kind} checked={runtimeKind === kind} onChange={() => { setRuntimeKind(kind); setRuntimeMode("native"); setDashboardPassword(""); }} className="peer sr-only" />
                <span className="flex h-full items-center gap-3 rounded-lg border border-input bg-background px-3 py-4 transition-colors hover:bg-muted peer-checked:border-primary peer-checked:bg-primary/10 peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-panel peer-disabled:cursor-not-allowed peer-disabled:opacity-50">
                  <RuntimeIcon kind={kind} className="size-9" /><span className="text-sm font-semibold">{name}</span>
                </span>
                <span aria-hidden="true" className="absolute right-2 top-2 size-1.5 rounded-full bg-primary opacity-0 peer-checked:opacity-100" />
              </label>)}
            </div>
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">{runtimeMode === "managed" ? "OpenClaw with model settings and conversations managed by Talos." : `Your own ${runtimes[runtimeKind]} workspace with the employee’s native tool permissions.`}</p>
          </fieldset>
          {runtimeKind === "openclaw" ? <label className="flex items-start gap-3 text-sm">
            <input type="checkbox" checked={runtimeMode === "managed"} onChange={(event) => setRuntimeMode(event.target.checked ? "managed" : "native")} disabled={writesDisabled} className="mt-0.5 size-4 accent-primary" />
            <span>Use Talos-managed conversations<span className="mt-1 block text-xs leading-relaxed text-muted-foreground">Use Talos model settings and saved conversations. Native tools are disabled in this mode.</span></span>
          </label> : <div>
            <label htmlFor="dashboard-password" className="mb-1.5 block text-xs text-muted-foreground">Hermes dashboard password</label>
            <input id="dashboard-password" type="password" autoComplete="new-password" className={inputClass} value={dashboardPassword} onChange={(event) => setDashboardPassword(event.target.value)} minLength={12} maxLength={256} required disabled={writesDisabled} aria-describedby="dashboard-password-help" />
            <p id="dashboard-password-help" className="mt-2 text-xs leading-relaxed text-muted-foreground">At least 12 characters. Sign in to Hermes as <span className="font-medium text-foreground">talos</span> with this password. Keep it in your password manager.</p>
          </div>}
          {runtimeMode === "native" ? <NativeModelChoice active={active} onConfigure={() => createDialog.current?.close()} value={createModel} onChange={setCreateModel} disabled={writesDisabled} /> : <p className="text-xs text-muted-foreground">Provider: {modelId === "fixture" ? "Local simulator" : "OpenRouter"} · Model: {modelId ?? "Loading…"} (workspace default)</p>}
          {(actionError || pollError) && <div role="alert" className="space-y-3 text-sm text-danger">
            <p>{actionError ?? pollError}</p>
            {retryRequest?.kind === "create" && <Button type="button" variant="outline" disabled={submitting} onClick={() => void mutate(retryRequest)}>Retry same request</Button>}
          </div>}
          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" variant="outline" disabled={submitting} onClick={() => createDialog.current?.close()}>Cancel</Button>
            <Button type="submit" disabled={writesDisabled || !displayName.trim() || (!employeeId && !employeeLabel.trim()) || (runtimeKind === "hermes" && dashboardPassword.length < 12)}>{submitting ? <LoaderCircle className="animate-spin" aria-hidden="true" /> : <Plus aria-hidden="true" />}{submitting ? "Creating…" : "Create agent"}</Button>
          </div>
        </form>
      </dialog>

      <div className="agent-layout">
        <aside className="agent-list-panel" aria-label="Agents">
          <div className="flex items-center justify-between border-b px-5 py-4"><h2 className="text-sm font-semibold">All agents</h2><Badge variant="secondary">{agents.length}</Badge></div>
          <div className="max-h-72 overflow-y-auto p-2">
            {loading ? <p className="px-3 py-6 text-sm text-muted-foreground">Loading agents…</p> : agents.length === 0 ? <p className="px-3 py-6 text-sm leading-relaxed text-muted-foreground">No agents yet. Select New agent to get started.</p> : agents.map((agent) => (
              <button key={agent.id} type="button" aria-pressed={selectedId === agent.id} onClick={() => { setSelectedId(agent.id); setAgentView("conversation"); }} className={cn("mb-1 flex w-full items-start gap-3 rounded-md px-3 py-3 text-left outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring", selectedId === agent.id && "bg-primary/10 text-primary") }>
                <RuntimeIcon kind={agent.runtime_kind} className="mt-0.5 size-6" />
                <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{agent.display_name}</span><span className="mt-1 block truncate text-xs text-muted-foreground">{agent.employee_name || "Unassigned"}</span><span className="mt-2 block"><StateBadge state={agent.observed_state} /></span></span>
              </button>
            ))}
          </div>
        </aside>

        <div className="min-w-0 bg-panel">
          {!selected ? <div className="flex min-h-96 flex-col items-center justify-center px-6 py-16 text-center lg:min-h-[480px]"><span className="mb-5 flex size-14 items-center justify-center rounded-xl border bg-muted/40"><Bot className="size-7 text-muted-foreground" strokeWidth={1.5} aria-hidden="true" /></span><h3 className="font-medium">No agent selected</h3><p className="mt-2 max-w-xs text-sm leading-relaxed text-muted-foreground">Create your first agent or select one from the list to manage its runtime.</p></div> : <>
            <div className="border-b p-6">
              <div className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0"><h2 className="break-words text-lg font-semibold tracking-tight">{selected.display_name}</h2><p className="mt-1 text-sm text-muted-foreground">{selected.employee_name || "Unassigned"}</p></div><StateBadge state={selected.observed_state} /></div>
              <p className="mt-3 break-words text-xs text-muted-foreground">{selected.runtime_mode === "native" ? `Native ${runtimeName} · ${selected.inference_override ? `OpenRouter · ${selected.inference_override.model_id}` : "Provider & model handled by agent"}` : `${selected.inference_override ? "Custom model" : "Workspace default"} · ${effectiveModel === "fixture" ? "Local simulator" : effectiveModel ?? "Loading…"}`}</p>
              <div className="mt-5 flex flex-wrap gap-2">
                <Button variant="outline" size="sm" disabled={writesDisabled || operationActive || ["ready", "running", "deleted"].includes(selected.observed_state)} onClick={() => lifecycle("start")}><Play aria-hidden="true" />Start</Button>
                <Button variant="outline" size="sm" disabled={writesDisabled || operationActive || ["stopped", "deleted"].includes(selected.observed_state)} onClick={() => lifecycle("stop")}><Square aria-hidden="true" />Stop</Button>
                <Button variant="outline" size="sm" className="ml-auto text-danger hover:bg-danger/10" disabled={writesDisabled || operationActive || selected.observed_state === "deleted"} onClick={() => lifecycle("delete")}><Trash2 aria-hidden="true" />Delete</Button>
              </div>
              {selectedOperation && <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground" aria-live="polite">{operationActive && <LoaderCircle className="size-3 animate-spin" aria-hidden="true" />}Latest operation: {selectedOperation.status.replaceAll("_", " ")}{selectedOperation.error && selectedOperation.error !== selected.last_error ? `. ${selectedOperation.error}` : ""}</p>}
              {selected.last_error && <p role="alert" className="mt-3 text-sm text-danger">{selected.last_error}</p>}
            </div>

            <nav aria-label="Agent views" className="flex gap-6 border-b px-6">
              {["conversation", "permissions", "settings"].map((view) => <button key={view} type="button" aria-pressed={agentView === view} onClick={() => setAgentView(view)} className={cn("border-b-2 px-1 py-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring", agentView === view ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}>{view === "conversation" ? "Test agent" : view === "permissions" ? "Permissions" : "Settings"}</button>)}
            </nav>
            {agentView === "permissions" && <div className="space-y-5 p-6">
              <div><label htmlFor="assigned-employee" className="mb-2 block text-sm font-medium">Employee assignment</label><select id="assigned-employee" className={inputClass} value={selected.employee_id ?? ""} disabled={writesDisabled || operationActive || selected.desired_state !== "stopped" || selected.observed_state !== "stopped"} onChange={(event) => void assignEmployee(event.target.value)}><option value="" disabled>Choose an employee</option>{employees.map((employee) => <option key={employee.id} value={employee.id}>{employee.name}</option>)}</select><p className="mt-2 text-xs text-muted-foreground">Stop the agent before changing its assignment. <a href="#employees" className="text-primary underline">Manage employees</a></p></div>
              {selected.runtime_mode === "managed" ? <p className="notice">Managed conversations have no native tools, regardless of the assigned role.</p> : selected.role ? <>
                <div className="flex flex-wrap items-center gap-3"><h3 className="font-semibold">{selected.role.name}</h3><Badge variant={selected.permissions_pending ? "warning" : "success"}>{selected.permissions_pending ? "Changes pending" : "Applied"}</Badge></div>
                <dl className="grid gap-5 sm:grid-cols-2"><div><dt className="text-sm font-medium">Saved permissions · revision {selected.role.revision}</dt><dd className="mt-2 text-sm text-muted-foreground">{selected.role.capabilities.map((id) => catalog.find((capability) => capability.id === id)?.name ?? id).join(", ") || "No tools"}</dd></div><div><dt className="text-sm font-medium">Applied permissions{selected.applied_role ? ` · ${selected.applied_role.name}, revision ${selected.applied_role.revision}` : ""}</dt><dd className="mt-2 text-sm text-muted-foreground">{selected.applied_role ? selected.applied_role.capabilities.map((id) => catalog.find((capability) => capability.id === id)?.name ?? id).join(", ") || "No tools" : "Not applied yet"}</dd></div></dl>
                <p className="text-sm text-muted-foreground">Apply interrupts running work and restarts the agent if it was running. Starting a stopped agent applies its current role automatically.</p>
                <Button variant="outline" disabled={writesDisabled || operationActive} onClick={() => lifecycle("apply-role")}>Apply saved role</Button>
                <p className="text-xs leading-relaxed text-muted-foreground">Terminal execution permits file and network operations even when dedicated tools are disabled. Native settings are a trusted administrator surface; direct edits there are outside role management.</p>
              </> : <p className="text-sm text-muted-foreground">This agent uses its existing native permissions. Assign an employee to manage it through a role.</p>}
            </div>}
            {active && agentView === "settings" && (selected.runtime_mode === "native" ? <div className="space-y-5 p-6">
              <NativeModelSettings key={selected.id} agentId={selected.id} modelId={selected.inference_override?.model_id ?? null} disabled={writesDisabled || operationActive} onOperation={onOperation} />
              <div><h3 className="flex items-center gap-3 font-semibold"><RuntimeIcon kind={selected.runtime_kind} className="size-8" />Your {runtimeName} workspace</h3><p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground">Open {runtimeName} to manage native settings, credentials and integrations. Your configuration and files persist across stops and starts.</p></div>
              <Button disabled={writesDisabled || operationActive || selected.observed_state !== "ready"} onClick={openNativeWorkspace}><ExternalLink aria-hidden="true" />{selectedOperation?.action === "dashboard" && operationActive ? `Opening ${runtimeName}…` : `Open ${runtimeName}`}</Button>
              {selectedOperation?.dashboard_url && <p className="text-sm"><a href={selectedOperation.dashboard_url} target="_blank" rel="noopener noreferrer" className="text-primary underline underline-offset-4">Continue if the new tab did not open</a></p>}
              <p className="max-w-xl text-sm leading-relaxed text-muted-foreground">{selected.observed_state !== "ready" ? "Start this agent to open its workspace." : `First visit: ${selected.runtime_kind === "hermes" ? "sign in as talos with the dashboard password you chose, then " : ""}configure a provider in ${runtimeName} if you selected “Handled by agent”.`}</p>
              <p className="max-w-xl text-xs leading-relaxed text-muted-foreground">Assigned agents use their applied role permissions. Saving a role does not change running agents. Remote Docker hosts require a tunnel for the workspace port.</p>
            </div> : <div className="p-6"><InferenceSettings key={selected.id} agentId={selected.id} onSaved={(selection) => setAgents((current) => current.map((agent) => agent.id === selected.id ? { ...agent, inference_override: selection.inherited ? null : selection } : agent))} /></div>)}
            <div className="p-6" hidden={agentView !== "conversation"}>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">Test agent</h3>{selectedRun && <StateBadge state={selectedRun.status} />}</div>
              <p className="mb-5 text-sm leading-relaxed text-muted-foreground">{selected.runtime_mode === "native" ? `Test ${runtimeName} as an administrator using its applied permissions and saved conversation. Configure its model in Settings.` : selected.model_route === "fixture"
                ? "This runtime uses the local simulator. Stop and start it once to enable the model picker."
                : effectiveModel === "fixture" ? "Local simulator selected. No external provider is called."
                : "Messages use this agent’s model configuration. Responses and events are saved by Talos."}</p>
              <div className="max-h-80 min-h-40 overflow-y-auto rounded-md border bg-background p-4" role="log" aria-label="Agent conversation" aria-live="polite">
                {selectedRuns.length ? [...selectedRuns].reverse().map((item) => <div key={item.id} className="mb-5 last:mb-0">
                  <p className="mb-1 text-xs font-medium text-muted-foreground">You</p>
                  <p className="mb-3 whitespace-pre-wrap break-words text-sm">{item.message}</p>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">{runtimeName}</p>
                  <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{item.output || item.error || (activeRuns.has(item.status) ? "Waiting for a response…" : `Message ${item.status.replaceAll("_", " ")}.`)}</p>
                </div>) : <p className="text-sm text-muted-foreground">Send a test message to check this agent’s behavior.</p>}
              </div>
              {selectedRun && <p className="mt-2 break-words text-xs text-muted-foreground">Model: {selectedRun.model_id === "native" ? `Configured in ${runtimeName}` : selectedRun.model_id === "fixture" ? "Local simulator" : selectedRun.model_id}</p>}
              {selectedRun && <UsageDetails calls={selectedRun.inference_calls ?? []} />}
              {selectedRun && !["fixture", "native"].includes(selectedRun.model_id) && <p className="mt-2 text-xs text-muted-foreground">Run settings: {Object.keys(selectedRun.inference?.settings ?? {}).length ? Object.entries(selectedRun.inference.settings!).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ") : "Model defaults"}</p>}
              {runEvents.length > 0 && <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{runEvents.length} recorded events</summary><ol className="mt-2 max-h-36 space-y-1 overflow-y-auto pl-4">{runEvents.map((event) => <li key={event.sequence}>{event.sequence}. {event.type.replaceAll("_", " ")}</li>)}</ol></details>}
              {selectedRun?.error && <p role="alert" className="mt-3 text-sm text-danger">{selectedRun.error}</p>}
              {selectedRun?.status === "unknown" && <p role="status" className="mt-3 text-sm text-warning">Delivery could not be confirmed. Talos will not resend this message automatically. Stop the agent before sending another message.</p>}
              <form onSubmit={sendDiagnostic} className="mt-5">
                <label htmlFor="diagnostic-message" className="mb-2 block text-sm font-medium">Message</label>
                <textarea id="diagnostic-message" rows={3} maxLength={4000} className={cn(inputClass, "resize-y")} placeholder="Message your agent" value={message} onChange={(event) => setMessage(event.target.value)} disabled={writesDisabled || runActive || operationActive || !["ready", "running"].includes(selected.observed_state)} required />
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><span className="text-xs text-muted-foreground">{!["ready", "running"].includes(selected.observed_state) ? "Start the agent to send a message." : "Output and events are saved by Talos."}</span><div className="flex gap-2">{runActive && selectedRun?.status !== "unknown" && <Button type="button" variant="outline" disabled={writesDisabled || selectedRun?.cancel_requested || selectedRun?.status === "cancel_requested"} onClick={() => void mutate({ path: `/runs/${runId}/cancel`, method: "POST", key: crypto.randomUUID(), kind: "cancel", agentId: selected.id })}><Square aria-hidden="true" />{selectedRun?.cancel_requested || selectedRun?.status === "cancel_requested" ? "Cancelling" : "Cancel run"}</Button>}<Button type="submit" disabled={writesDisabled || runActive || operationActive || !message.trim() || !["ready", "running"].includes(selected.observed_state)}><Send aria-hidden="true" />Send</Button></div></div>
              </form>
            </div>
          </>}
        </div>
      </div>
    </section>
  );
}
