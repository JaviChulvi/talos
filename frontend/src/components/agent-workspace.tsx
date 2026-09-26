import { useEffect, useRef, useState, type FormEvent } from "react";
import { Bot, CircleAlert, LoaderCircle, Play, Plus, Send, Square, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { GenerationControls, UsageDetails, type GenerationSettings, type ModelCapabilities, type InferenceSelection, type InferenceCall } from "@/components/generation-settings";
import { ModelPicker } from "@/components/model-picker";
import { api, ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

type Agent = {
  id: string;
  display_name: string;
  employee_label: string;
  desired_state: string;
  observed_state: string;
  last_error: string | null;
  model_route: string;
};
type Operation = { id: string; agent_id: string; status: string; error?: string | null };
type Run = { inference_calls: InferenceCall[]; inference: { settings?: GenerationSettings }; model_id: string; id: string; agent_id: string; status: string; output?: string | null; error?: string | null; cancel_requested?: boolean };
type RunEvent = { sequence: number; type: string; payload: Record<string, unknown> };
type Mutation = {
  path: string;
  method: "POST" | "DELETE";
  body?: object;
  key: string;
  kind: "create" | "lifecycle" | "diagnostic" | "cancel";
  agentId?: string;
};

const activeOperations = new Set(["queued", "running", "retry_wait"]);
const activeRuns = new Set(["queued", "dispatching", "running", "cancel_requested", "unknown"]);
const transitionalStates = new Set(["pending", "provisioning", "starting", "stopping", "deleting"]);
const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

function readIds(key: string): Record<string, string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) ?? "{}");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return Object.fromEntries(Object.entries(value).filter(([, id]) => typeof id === "string"));
    }
  } catch { /* Stored identifiers are optional; the server owns all records. */ }
  return {};
}

function saveIds(key: string, ids: Record<string, string>) {
  try { localStorage.setItem(key, JSON.stringify(ids)); } catch { /* Continue when local storage is disabled. */ }
}

function StateBadge({ state }: { state: string }) {
  const healthy = ["ready", "running", "completed", "succeeded"].includes(state);
  const warning = ["unknown", "failed", "error", "interrupted"].includes(state);
  return <Badge variant={healthy ? "success" : warning ? "warning" : "secondary"} className="capitalize">{state.replaceAll("_", " ")}</Badge>;
}

export function AgentWorkspace() {
  const [modelId, setModelId] = useState<string | null>(null);
  const [draftModel, setDraftModel] = useState<string | null>(null);
  const [models, setModels] = useState<ModelCapabilities[]>([]);
  const [savedSettings, setSavedSettings] = useState<GenerationSettings>({});
  const [draftSettings, setDraftSettings] = useState<GenerationSettings | null>(null);
  const [savedCapabilities, setSavedCapabilities] = useState<Partial<ModelCapabilities>>({});
  const [modelError, setModelError] = useState<string | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogRefresh, setCatalogRefresh] = useState(0);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [operationIds, setOperationIds] = useState(() => readIds("talos.operationIds"));
  const [runIds, setRunIds] = useState(() => readIds("talos.runIds"));
  const [operation, setOperation] = useState<Operation | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<{ runId: string; items: RunEvent[] }>({ runId: "", items: [] });
  const cursor = useRef({ runId: "", sequence: 0 });
  const [loading, setLoading] = useState(true);
  const [pollError, setPollError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [retryRequest, setRetryRequest] = useState<Mutation | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [displayName, setDisplayName] = useState("");
  const [employeeLabel, setEmployeeLabel] = useState("");
  const [message, setMessage] = useState("");

  const selected = agents.find((agent) => agent.id === selectedId);
  const operationId = operationIds[selectedId];
  const runId = runIds[selectedId];
  const selectedOperation = operation?.id === operationId ? operation : null;
  const selectedRun = run?.id === runId ? run : null;
  const operationActive = !!selectedOperation && activeOperations.has(selectedOperation.status);
  const runActive = !!selectedRun && activeRuns.has(selectedRun.status);
  const busy = operationActive || runActive || agents.some((agent) => transitionalStates.has(agent.observed_state));
  const writesDisabled = submitting || !!retryRequest || !!pollError || loading;
  const runEvents = events.runId === runId ? events.items : [];

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setCatalogLoading(true);
      api<{ models: ModelCapabilities[] }>("/inference/models", {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
      }).then((data) => { if (!controller.signal.aborted) { setModels(data.models); setCatalogError(null); } })
        .catch((error) => { if (!controller.signal.aborted) setCatalogError(errorMessage(error)); })
        .finally(() => { if (!controller.signal.aborted) setCatalogLoading(false); });
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [catalogRefresh]);

  async function saveModel(event: FormEvent) {
    event.preventDefault();
    if (!modelId || savingModel) return;
    setSavingModel(true);
    setModelError(null);
    try {
      const result = await api<InferenceSelection>("/inference", {
        method: "PUT", body: JSON.stringify({ model_id: draftModel ?? modelId, settings: draftSettings ?? savedSettings }), signal: AbortSignal.timeout(15_000),
      });
      setModelId(result.model_id);
      setSavedSettings(result.settings);
      setSavedCapabilities(result.capabilities);
      setDraftModel(null);
      setDraftSettings(null);
      setRefresh((value) => value + 1);
    } catch (error) { setModelError(`${errorMessage(error)} Check the active model below before retrying.`); }
    finally { setSavingModel(false); }
  }

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
        const latestRun = runId ? api<Run>(`/runs/${runId}`, options).catch((error) => {
          if (error instanceof ApiError && error.status === 404) return null;
          throw error;
        }) : Promise.resolve(null);
        const [nextAgents, nextOperation, nextRun, nextModel] = await Promise.all([
          api<Agent[]>("/agents", options), latestOperation, latestRun, api<InferenceSelection>("/inference", options),
        ]);
        const after = cursor.current.runId === runId ? cursor.current.sequence : 0;
        const nextEvents = nextRun ? await api<RunEvent[]>(`/runs/${runId}/events?after=${after}`, options) : [];
        if (stopped) return;
        setAgents(nextAgents);
        setModelId(nextModel.model_id);
        setSavedSettings(nextModel.settings);
        setSavedCapabilities(nextModel.capabilities);
        setSelectedId((current) => nextAgents.some((agent) => agent.id === current) ? current : nextAgents[0]?.id ?? "");
        setOperation(nextOperation);
        setRun(nextRun);
        if (runId && nextRun) {
          setEvents((current) => ({ runId, items: current.runId === runId ? [...current.items, ...nextEvents] : nextEvents }));
          cursor.current = { runId, sequence: nextEvents.at(-1)?.sequence ?? after };
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
  }, [selectedId, operationId, runId, busy, refresh]);

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
      if (request.kind === "diagnostic") {
        const next = { ...runIds, [result.agent_id]: result.id };
        setRunIds(next);
        saveIds("talos.runIds", next);
        setRun(result as Run);
        setMessage("");
      } else if (request.kind !== "cancel") {
        const next = { ...operationIds, [result.agent_id]: result.id };
        setOperationIds(next);
        saveIds("talos.operationIds", next);
        setOperation(result);
        setSelectedId(result.agent_id);
        if (request.kind === "create") { setDisplayName(""); setEmployeeLabel(""); }
      }
      setRetryRequest(null);
      setRefresh((value) => value + 1);
    } catch (error) {
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
    if (!displayName.trim() || !employeeLabel.trim() || employeeLabel.trim().length > 160 || writesDisabled) return;
    void mutate({ path: "/agents", method: "POST", key: crypto.randomUUID(), kind: "create", body: {
      display_name: displayName.trim(), employee_label: employeeLabel.trim(),
    } });
  }

  function lifecycle(action: "start" | "stop" | "delete") {
    if (!selected || writesDisabled || operationActive) return;
    if (action === "delete" && !window.confirm(`Delete ${selected.display_name}? This removes its runtime and private agent state. This cannot be undone.`)) return;
    void mutate({ path: `/agents/${selected.id}${action === "delete" ? "" : `/${action}`}`, method: action === "delete" ? "DELETE" : "POST", key: crypto.randomUUID(), kind: "lifecycle", agentId: selected.id });
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
        <Button onClick={() => document.getElementById("agent-name")?.focus()}><Plus aria-hidden="true" />New agent</Button>
      </div>
      {(actionError || pollError) && <div role="alert" className="notice notice-warning mb-5 flex-wrap">
        <CircleAlert className="size-4 shrink-0" aria-hidden="true" />
        <p className="min-w-0 flex-1">{actionError ?? pollError}</p>
        {retryRequest && <Button variant="outline" size="sm" disabled={submitting} onClick={() => void mutate(retryRequest)}>Retry same request</Button>}
      </div>}

      <form onSubmit={saveModel} className="mb-6 border-y py-5" aria-label="Default model">
        <h2 className="mb-4 text-sm font-semibold">Model for all agents</h2>
        <div className="flex flex-wrap items-end gap-3">
          <ModelPicker models={models} value={draftModel ?? modelId ?? "fixture"} onChange={(id) => { setDraftModel(id); setDraftSettings({}); }} disabled={savingModel || modelId === null} inputClass={inputClass} />
          <Button type="submit" disabled={savingModel || modelId === null}>{savingModel ? "Saving…" : "Apply settings"}</Button>
          <Button type="button" variant="outline" disabled={catalogLoading} onClick={() => setCatalogRefresh((value) => value + 1)}>{catalogLoading ? "Loading models…" : "Reload models"}</Button>
        </div>
        <p id="model-help" className="mt-3 text-xs leading-relaxed text-muted-foreground">Applies to new messages. Running and queued requests keep their original model and settings. OpenRouter requires a gateway API key.</p>
        <p className="mt-2 break-words text-sm text-muted-foreground" role="status">Active selection: {modelId === null ? "Loading…" : modelId === "fixture" ? "Local simulator" : modelId}</p>
        {(draftModel ?? modelId) !== "fixture" && <GenerationControls
          model={models.find((m) => m.id === (draftModel ?? modelId)) ?? (savedCapabilities.id === (draftModel ?? modelId) ? savedCapabilities as ModelCapabilities : undefined)}
          settings={draftSettings ?? savedSettings} onChange={setDraftSettings} disabled={savingModel || modelId === null} inputClass={inputClass} />}
        {(modelError || catalogError) && <p role="alert" className="mt-2 text-sm text-danger">{modelError ?? catalogError}</p>}
      </form>

      <div className="agent-layout">
        <aside className="agent-list-panel" aria-label="Agents">
          <div className="flex items-center justify-between border-b px-5 py-4"><h2 className="text-sm font-semibold">All agents</h2><Badge variant="secondary">{agents.length}</Badge></div>
          <div className="max-h-72 overflow-y-auto p-2">
            {loading ? <p className="px-3 py-6 text-sm text-muted-foreground">Loading agents…</p> : agents.length === 0 ? <p className="px-3 py-6 text-sm leading-relaxed text-muted-foreground">No agents yet. Create one below to get started.</p> : agents.map((agent) => (
              <button key={agent.id} type="button" aria-pressed={selectedId === agent.id} onClick={() => setSelectedId(agent.id)} className={cn("mb-1 flex w-full items-start gap-3 rounded-md px-3 py-3 text-left outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring", selectedId === agent.id && "bg-primary/10 text-primary") }>
                <Bot className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{agent.display_name}</span><span className="mt-1 block truncate text-xs text-muted-foreground">{agent.employee_label || "Unassigned"}</span><span className="mt-2 block"><StateBadge state={agent.observed_state} /></span></span>
              </button>
            ))}
          </div>
          <form onSubmit={createAgent} className="space-y-4 border-t p-5">
            <h3 className="text-sm font-semibold">Create an agent</h3>
            <div><label htmlFor="agent-name" className="mb-1.5 block text-xs text-muted-foreground">Name</label><input id="agent-name" className={inputClass} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Sales assistant" maxLength={120} required disabled={writesDisabled} /></div>
            <div><label htmlFor="employee-label" className="mb-1.5 block text-xs text-muted-foreground">Employee label</label><input id="employee-label" className={inputClass} value={employeeLabel} onChange={(event) => setEmployeeLabel(event.target.value)} placeholder="Alex" required minLength={1} maxLength={160} disabled={writesDisabled} /></div>
            <Button type="submit" className="w-full" disabled={writesDisabled || !displayName.trim() || !employeeLabel.trim()}><Plus aria-hidden="true" />Create agent</Button>
          </form>
        </aside>

        <div className="min-w-0 bg-panel">
          {!selected ? <div className="flex min-h-96 flex-col items-center justify-center px-6 py-16 text-center lg:min-h-[480px]"><span className="mb-5 flex size-14 items-center justify-center rounded-xl border bg-muted/40"><Bot className="size-7 text-muted-foreground" strokeWidth={1.5} aria-hidden="true" /></span><h3 className="font-medium">No agent selected</h3><p className="mt-2 max-w-xs text-sm leading-relaxed text-muted-foreground">Create your first agent or select one from the list to manage its runtime.</p></div> : <>
            <div className="border-b p-6">
              <div className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0"><h2 className="break-words text-lg font-semibold tracking-tight">{selected.display_name}</h2><p className="mt-1 text-sm text-muted-foreground">{selected.employee_label || "No employee label"}</p></div><StateBadge state={selected.observed_state} /></div>
              <div className="mt-5 flex flex-wrap gap-2">
                <Button variant="outline" size="sm" disabled={writesDisabled || operationActive || ["ready", "running", "deleted"].includes(selected.observed_state)} onClick={() => lifecycle("start")}><Play aria-hidden="true" />Start</Button>
                <Button variant="outline" size="sm" disabled={writesDisabled || operationActive || ["stopped", "deleted"].includes(selected.observed_state)} onClick={() => lifecycle("stop")}><Square aria-hidden="true" />Stop</Button>
                <Button variant="outline" size="sm" className="ml-auto text-danger hover:bg-danger/10" disabled={writesDisabled || operationActive || selected.observed_state === "deleted"} onClick={() => lifecycle("delete")}><Trash2 aria-hidden="true" />Delete</Button>
              </div>
              {selectedOperation && <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground" aria-live="polite">{operationActive && <LoaderCircle className="size-3 animate-spin" aria-hidden="true" />}Latest operation: {selectedOperation.status.replaceAll("_", " ")}{selectedOperation.error && selectedOperation.error !== selected.last_error ? `. ${selectedOperation.error}` : ""}</p>}
              {selected.last_error && <p role="alert" className="mt-3 text-sm text-danger">{selected.last_error}</p>}
            </div>

            <div className="p-6">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">Diagnostic conversation</h3>{selectedRun && <StateBadge state={selectedRun.status} />}</div>
              <p className="mb-5 text-sm leading-relaxed text-muted-foreground">{selected.model_route === "fixture"
                ? "This runtime uses the local simulator. Stop and start it once to enable the model picker."
                : modelId === "fixture" ? "Local simulator selected. No external provider is called."
                : "Messages use the selected OpenRouter model. Responses and events are saved by Talos."}</p>
              <div className="max-h-80 min-h-40 overflow-y-auto rounded-md border bg-background p-4" role="log" aria-label="Recorded diagnostic output" aria-live="polite">
                {selectedRun?.output ? <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{selectedRun.output}</p> : <p className="text-sm text-muted-foreground">{runActive ? "Waiting for recorded output…" : "Send a message to see recorded output here."}</p>}
              </div>
              {selectedRun && <p className="mt-2 break-words text-xs text-muted-foreground">Recorded model: {selectedRun.model_id === "fixture" ? "Local simulator" : selectedRun.model_id}</p>}
              {selectedRun && <UsageDetails calls={selectedRun.inference_calls ?? []} />}
              {selectedRun && selectedRun.model_id !== "fixture" && <p className="mt-2 text-xs text-muted-foreground">Run settings: {Object.keys(selectedRun.inference?.settings ?? {}).length ? Object.entries(selectedRun.inference.settings!).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ") : "Model defaults"}</p>}
              {runEvents.length > 0 && <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{runEvents.length} recorded events</summary><ol className="mt-2 max-h-36 space-y-1 overflow-y-auto pl-4">{runEvents.map((event) => <li key={event.sequence}>{event.sequence}. {event.type.replaceAll("_", " ")}</li>)}</ol></details>}
              {selectedRun?.error && <p role="alert" className="mt-3 text-sm text-danger">{selectedRun.error}</p>}
              {selectedRun?.status === "unknown" && <p role="status" className="mt-3 text-sm text-warning">Delivery could not be confirmed. Talos will not resend this message automatically. Stop the agent before starting another diagnostic.</p>}
              <form onSubmit={sendDiagnostic} className="mt-5">
                <label htmlFor="diagnostic-message" className="mb-2 block text-sm font-medium">Message</label>
                <textarea id="diagnostic-message" rows={3} maxLength={4000} className={cn(inputClass, "resize-y")} placeholder="Send a test message to your agent" value={message} onChange={(event) => setMessage(event.target.value)} disabled={writesDisabled || runActive || operationActive || !["ready", "running"].includes(selected.observed_state)} required />
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><span className="text-xs text-muted-foreground">{!["ready", "running"].includes(selected.observed_state) ? "Start the agent to send a diagnostic." : "Output and events are saved by Talos."}</span><div className="flex gap-2">{runActive && selectedRun?.status !== "unknown" && <Button type="button" variant="outline" disabled={writesDisabled || selectedRun?.cancel_requested || selectedRun?.status === "cancel_requested"} onClick={() => void mutate({ path: `/runs/${runId}/cancel`, method: "POST", key: crypto.randomUUID(), kind: "cancel", agentId: selected.id })}><Square aria-hidden="true" />{selectedRun?.cancel_requested || selectedRun?.status === "cancel_requested" ? "Cancelling" : "Cancel run"}</Button>}<Button type="submit" disabled={writesDisabled || runActive || operationActive || !message.trim() || !["ready", "running"].includes(selected.observed_state)}><Send aria-hidden="true" />Send</Button></div></div>
              </form>
            </div>
          </>}
        </div>
      </div>
    </section>
  );
}
