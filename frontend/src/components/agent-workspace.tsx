import { AgentBudget } from "@/components/employee-budget";
import { AgentHandoff } from "@/components/agent-handoff";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ExternalLink,
  Bot,
  CircleAlert,
  LoaderCircle,
  Play,
  Plus,
  ArrowLeft,
  ArrowUp,
  Square,
  Trash2,
  MoreHorizontal,
  Settings2,
  ChevronDown,
  Users,
  ShieldCheck,
  Settings,
  Activity,
  MessageSquare,
  PanelLeftClose,
  Package,
  DollarSign,
  ListChecks,
} from "lucide-react";
import {
  Sidebar,
  SidebarHeader,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarInset,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupTextarea,
} from "@/components/ui/input-group";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  UsageDetails,
  type GenerationSettings,
  type InferenceSelection,
  type InferenceCall,
} from "@/components/generation-settings";
import {
  InferenceSettings,
  NativeModelChoice,
  NativeModelSettings,
} from "@/components/inference-settings";
import {
  api,
  ApiError,
  errorMessage,
  type Employee,
  type Capability,
  type AgentPermissions,
  type SetupPreview,
} from "@/lib/api";
import {
  ApplyPreview,
  CaptureSetup,
  SetupStatus,
} from "@/components/setup-controls";
import { cn } from "@/lib/utils";

type Agent = AgentPermissions & {
  id: string;
  runtime_kind: "openclaw" | "hermes";
  runtime_mode: "native" | "managed";
  runtime_release: string;
  display_name: string;
  employee_label: string;
  desired_state: string;
  observed_state: string;
  last_error: string | null;
  model_route: string;
  inference_override: InferenceSelection | null;
};
type Operation = {
  result?: { setup_id?: string } | null;
  action?: string;
  dashboard_url?: string | null;
  id: string;
  agent_id: string;
  status: string;
  error?: string | null;
};
type Run = {
  message: string;
  inference_calls: InferenceCall[];
  inference: { settings?: GenerationSettings };
  model_id: string;
  id: string;
  agent_id: string;
  status: string;
  output?: string | null;
  error?: string | null;
  cancel_requested?: boolean;
};
type RunEvent = {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
};
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
type RuntimeTarget = { runtime_kind: RuntimeKind; runtime_release: string };

function RuntimeIcon({
  kind,
  className,
}: {
  kind: RuntimeKind;
  className?: string;
}) {
  return (
    <img
      src={`/runtime-icons/${kind}.svg`}
      alt=""
      aria-hidden="true"
      className={cn("shrink-0 object-contain", className)}
    />
  );
}

const activeOperations = new Set(["queued", "running", "retry_wait"]);
const activeRuns = new Set([
  "queued",
  "dispatching",
  "running",
  "cancel_requested",
  "unknown",
]);
const transitionalStates = new Set([
  "pending",
  "provisioning",
  "starting",
  "stopping",
  "deleting",
  "applying",
]);

function StateBadge({ state }: { state: string }) {
  const healthy = ["ready", "running", "completed", "succeeded"].includes(
    state,
  );
  const warning = ["unknown", "failed", "error", "interrupted"].includes(state);
  return (
    <Badge
      variant={healthy ? "success" : warning ? "warning" : "secondary"}
      className="capitalize"
    >
      {state.replaceAll("_", " ")}
    </Badge>
  );
}

export function AgentWorkspace({
  page,
  connected,
  version,
  children,
  operationIds,
  onOperation,
  onSignOut,
  signingOut,
}: {
  page: string;
  connected: boolean;
  version?: string;
  children: ReactNode;
  operationIds: Record<string, string>;
  onOperation: (agentId: string, id: string) => void;
  onSignOut: () => void;
  signingOut: boolean;
}) {
  const settingsAgentId = /^agents\/([^/]+)\/settings$/.exec(page)?.[1];
  const settingsActive = !!settingsAgentId;
  const active = page === "agents" || settingsActive;
  const { setOpenMobile } = useSidebar();
  const [agentView, setAgentView] = useState("settings");
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmation, setConfirmation] = useState<{
    action: "delete" | "apply-role";
    agentId: string;
  } | null>(null);
  const [applyPreview, setApplyPreview] = useState<SetupPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const previewRequest = useRef(0);
  const conversation = useRef<HTMLDivElement>(null);
  const actionsTrigger = useRef<HTMLButtonElement>(null);
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const createTrigger = useRef<HTMLElement | null>(null);
  const navigationTrigger = useRef<HTMLButtonElement>(null);
  const [modelId, setModelId] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [chatAgentId, setChatAgentId] = useState(settingsAgentId ?? "");
  const selectedId = settingsAgentId ?? chatAgentId;
  const [operation, setOperation] = useState<Operation | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<{ runId: string; items: RunEvent[] }>({
    runId: "",
    items: [],
  });
  const cursor = useRef({ runId: "", sequence: 0 });
  const [loading, setLoading] = useState(true);
  const [pollError, setPollError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [retryRequest, setRetryRequest] = useState<Mutation | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [runtimeKind, setRuntimeKind] = useState<RuntimeKind>("openclaw");
  const [runtimeVersion, setRuntimeVersion] = useState("latest");
  const [runtimeTargets, setRuntimeTargets] = useState<RuntimeTarget[]>([]);
  const [runtimeTargetsError, setRuntimeTargetsError] = useState("");
  const [createModel, setCreateModel] = useState<string | null>(null);
  const [dashboardPassword, setDashboardPassword] = useState("");
  const [runtimeMode, setRuntimeMode] = useState<"native" | "managed">(
    "native",
  );
  const runtimeChoices = runtimeTargets.filter(
    (target) => target.runtime_kind === runtimeKind,
  );
  useEffect(() => {
    if (!createOpen) return;
    const controller = new AbortController();
    api<RuntimeTarget[]>("/setups/runtime-targets", {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
    })
      .then(setRuntimeTargets)
      .catch((cause) => {
        if (!controller.signal.aborted)
          setRuntimeTargetsError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [createOpen]);

  function changeCreateOpen(open: boolean) {
    if (submitting) return;
    if (open) {
      setRuntimeVersion("latest");
      setRuntimeTargets([]);
      setRuntimeTargetsError("");
    }
    setCreateOpen(open);
  }
  const dashboardWindows = useRef<Record<string, Window>>({});
  const [displayName, setDisplayName] = useState("");
  const [employeeLabel, setEmployeeLabel] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [catalog, setCatalog] = useState<Capability[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [historyLoading, setHistoryLoading] = useState(true);
  const message = drafts[selectedId] ?? "";

  const selected = agents.find((agent) => agent.id === selectedId);
  const hasSelected = !!selected;
  useEffect(() => {
    if (settingsActive && hasSelected)
      document.getElementById("agent-settings-heading")?.focus();
  }, [settingsActive, hasSelected, selectedId]);
  const runtimeName = runtimes[selected?.runtime_kind ?? "openclaw"];
  const effectiveModel = selected?.inference_override?.model_id ?? modelId;
  const operationId = operationIds[selectedId];
  const selectedRuns = runs.filter((run) => run.agent_id === selectedId);
  const selectedRun = selectedRuns[0];
  const runId = selectedRun?.id;
  const selectedOperation = operation?.id === operationId ? operation : null;
  const operationActive =
    (!!selectedOperation && activeOperations.has(selectedOperation.status)) ||
    transitionalStates.has(selected?.observed_state ?? "");
  const runActive = !!selectedRun && activeRuns.has(selectedRun.status);
  const busy =
    operationActive ||
    runActive ||
    agents.some((agent) => transitionalStates.has(agent.observed_state));
  const writesDisabled = submitting || !!retryRequest || !!pollError || loading;
  const runEvents = events.runId === runId ? events.items : [];
  const lastTool = runEvents.filter((event) => event.type === "tool").at(-1);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let stopped = false;
    const poll = async () => {
      try {
        const options = {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(8000),
          ]),
        };
        const latestOperation = operationId
          ? api<Operation>(`/operations/${operationId}`, options).catch(
              (error) => {
                if (error instanceof ApiError && error.status === 404)
                  return null;
                throw error;
              },
            )
          : Promise.resolve(null);
        const [
          nextAgents,
          nextOperation,
          nextRuns,
          nextModel,
          dashboards,
          nextEmployees,
          nextCatalog,
        ] = await Promise.all([
          api<Agent[]>("/agents", options),
          latestOperation,
          selectedId
            ? api<Run[]>(`/agents/${selectedId}/runs`, options)
            : Promise.resolve([]),
          api<InferenceSelection>("/inference", options),
          Promise.all(
            Object.keys(dashboardWindows.current).map((id) =>
              id === operationId
                ? latestOperation
                : api<Operation>(`/operations/${id}`, options),
            ),
          ),
          api<Employee[]>("/employees", options),
          api<Capability[]>("/capabilities", options),
        ]);
        const nextRun = nextRuns[0];
        const nextRunId = nextRun?.id;
        const after =
          cursor.current.runId === nextRunId ? cursor.current.sequence : 0;
        const nextEvents = nextRun
          ? await api<RunEvent[]>(
              `/runs/${nextRunId}/events?after=${after}`,
              options,
            )
          : [];
        if (stopped) return;
        for (const dashboard of dashboards) {
          if (!dashboard || activeOperations.has(dashboard.status)) continue;
          const popup = dashboardWindows.current[dashboard.id];
          if (popup && !popup.closed) {
            if (dashboard.dashboard_url)
              popup.location.replace(dashboard.dashboard_url);
            else popup.close();
          }
          if (dashboard.error) setActionError(dashboard.error);
          delete dashboardWindows.current[dashboard.id];
        }
        setAgents(nextAgents);
        setEmployees(nextEmployees);
        setCatalog(nextCatalog);
        setModelId(nextModel.model_id);
        const nextSelectedId = nextAgents.some(
          (agent) => agent.id === selectedId,
        )
          ? selectedId
          : (nextAgents[0]?.id ?? "");
        setChatAgentId(nextSelectedId);
        setHistoryLoading(!!nextSelectedId && nextSelectedId !== selectedId);
        setOperation(nextOperation);
        setRuns(nextRuns);
        if (nextRunId) {
          setEvents((current) => ({
            runId: nextRunId,
            items:
              current.runId === nextRunId
                ? [...current.items, ...nextEvents]
                : nextEvents,
          }));
          cursor.current = {
            runId: nextRunId,
            sequence: nextEvents.at(-1)?.sequence ?? after,
          };
        }
        setPollError(null);
      } catch (error) {
        if (!stopped)
          setPollError(
            `${errorMessage(error)} Displayed records may be out of date.`,
          );
      } finally {
        if (!stopped) {
          setLoading(false);
          timer = window.setTimeout(() => void poll(), busy ? 2000 : 5000);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [selectedId, operationId, runId, busy, refresh, active]);

  useEffect(() => {
    conversation.current?.scrollTo({ top: conversation.current.scrollHeight });
  }, [selectedId, runId, selectedRun?.output, active]);

  async function mutate(request: Mutation) {
    if (request.kind === "dashboard" && (!request.popup || request.popup.closed)) {
      request.popup = window.open("about:blank", "_blank");
      if (request.popup) request.popup.opener = null;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      const result = await api<Operation | Run>(request.path, {
        method: request.method,
        headers: { "Idempotency-Key": request.key },
        body: request.body ? JSON.stringify(request.body) : undefined,
        signal: AbortSignal.timeout(15_000),
      });
      if (
        request.kind === "dashboard" &&
        request.popup &&
        !request.popup.closed
      )
        dashboardWindows.current[result.id] = request.popup;
      if (request.kind === "diagnostic") {
        setRuns((current) => [
          result as Run,
          ...current.filter((run) => run.id !== result.id),
        ]);
        setDrafts((current) => ({ ...current, [request.agentId!]: "" }));
      } else if (request.kind !== "cancel") {
        onOperation(result.agent_id, result.id);
        setOperation(result);
        if (result.agent_id !== selectedId) setHistoryLoading(true);
        setChatAgentId(result.agent_id);
        if (request.kind === "create") {
          setDisplayName("");
          setEmployeeLabel("");
          setEmployeeId("");
          setDashboardPassword("");
          setCreateOpen(false);
          window.location.assign("#agents");
        }
      }
      setRetryRequest(null);
      setRefresh((value) => value + 1);
    } catch (error) {
      if (request.kind === "dashboard") request.popup?.close();
      const uncertain = !(error instanceof ApiError) || error.status >= 500;
      setRetryRequest(uncertain ? request : null);
      setActionError(
        uncertain
          ? "The request result is unknown. Retry the same request to recover its result without creating a duplicate."
          : errorMessage(error),
      );
    } finally {
      setSubmitting(false);
    }
  }

  function createAgent(event: FormEvent) {
    event.preventDefault();
    if (
      !displayName.trim() ||
      (!employeeId && !employeeLabel.trim()) ||
      employeeLabel.trim().length > 160 ||
      (runtimeMode === "native" &&
        (!runtimeChoices.length || !!runtimeTargetsError)) ||
      writesDisabled
    )
      return;
    void mutate({
      path: "/agents",
      method: "POST",
      key: crypto.randomUUID(),
      kind: "create",
      body: {
        display_name: displayName.trim(),
        ...(employeeId
          ? { employee_id: employeeId }
          : { employee_label: employeeLabel.trim() }),
        runtime_mode: runtimeMode,
        runtime_kind: runtimeKind,
        ...(runtimeMode === "native" ? { runtime_version: runtimeVersion } : {}),
        ...(runtimeMode === "native" && createModel
          ? { model_id: createModel }
          : {}),
        ...(runtimeKind === "hermes"
          ? { dashboard_password: dashboardPassword }
          : {}),
      },
    });
  }

  function lifecycle(
    action: "start" | "stop" | "delete" | "apply-role",
    confirmed = false,
    agent = selected,
  ) {
    if (
      !agent ||
      writesDisabled ||
      transitionalStates.has(agent.observed_state) ||
      (agent.id === selectedId && operationActive)
    )
      return;
    if (!confirmed && (action === "delete" || action === "apply-role")) {
      confirmationTrigger.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      setConfirmation({ action, agentId: agent.id });
      if (action === "apply-role") {
        const request = ++previewRequest.current;
        setApplyPreview(null);
        setPreviewLoading(true);
        void api<SetupPreview>(`/agents/${agent.id}/setup-preview`, {
          method: "POST",
          signal: AbortSignal.timeout(15_000),
        })
          .then((preview) => {
            if (previewRequest.current === request) setApplyPreview(preview);
          })
          .catch((cause) => {
            if (previewRequest.current === request)
              setApplyPreview({
                application: null,
                changes: [],
                blockers: [errorMessage(cause)],
              });
          })
          .finally(() => {
            if (previewRequest.current === request) setPreviewLoading(false);
          });
      }
      return;
    }
    setConfirmation(null);
    void mutate({
      path: `/agents/${agent.id}${action === "delete" ? "" : `/${action}`}`,
      method: action === "delete" ? "DELETE" : "POST",
      key: crypto.randomUUID(),
      kind: "lifecycle",
      agentId: agent.id,
    });
  }

  async function assignEmployee(id: string) {
    if (!selected || !id || writesDisabled) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const updated = await api<Agent>(`/agents/${selected.id}/employee`, {
        method: "PUT",
        body: JSON.stringify({ employee_id: id }),
      });
      setAgents((current) =>
        current.map((agent) => (agent.id === updated.id ? updated : agent)),
      );
    } catch (cause) {
      setActionError(errorMessage(cause));
    } finally {
      setSubmitting(false);
    }
  }

  function openNativeWorkspace() {
    if (!selected || writesDisabled || operationActive) return;
    void mutate({
      path: `/agents/${selected.id}/dashboard`,
      method: "POST",
      key: crypto.randomUUID(),
      kind: "dashboard",
      agentId: selected.id,
    });
  }

  function sendDiagnostic(event: FormEvent) {
    event.preventDefault();
    if (
      !selected ||
      !message.trim() ||
      writesDisabled ||
      runActive ||
      operationActive ||
      historyLoading ||
      !["ready", "running"].includes(selected.observed_state)
    )
      return;
    void mutate({
      path: `/agents/${selected.id}/diagnostic-runs`,
      method: "POST",
      key: crypto.randomUUID(),
      kind: "diagnostic",
      agentId: selected.id,
      body: { message: message.trim() },
    });
  }

  const navigate = () => {
    setOpenMobile(false);
  };
  const selectAgent = (id: string) => {
    if (id !== selectedId) setHistoryLoading(true);
    setChatAgentId(id);
    window.location.assign("#agents");
    navigate();
  };
  const openAgentSettings = (id: string) => {
    if (id !== selectedId) setHistoryLoading(true);
    setChatAgentId(id);
    setAgentView("settings");
    setOpenMobile(false);
    window.location.assign(`#agents/${id}/settings`);
  };
  const budgetCoverage = selected?.runtime_mode === "native"
    ? selected.inference_override ? "gateway" : "external"
    : effectiveModel === "fixture" ? "simulator" : "gateway";
  const pageTitle = page.startsWith("onboarding")
    ? "Employee setup"
    : page.startsWith("setups")
    ? "Setups"
    : page.startsWith("usage")
      ? "Usage"
      : page === "employees"
      ? "Employees"
      : page === "roles"
        ? "Roles"
        : page === "settings"
          ? "Settings"
          : "Platform status";
  const available =
    selected && ["ready", "running"].includes(selected.observed_state);
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <Sidebar collapsible="offcanvas" className="border-r-0">
        <SidebarHeader className="gap-5 px-3 pt-4">
          <div className="flex h-8 items-center justify-between px-2">
            <a
              href="#agents"
              onClick={navigate}
              className="flex items-center gap-2.5 font-semibold"
              aria-label="Talos agents"
            >
              <img src="/talos.svg" className="size-6" alt="" />
              Talos
            </a>
            <Button
              variant="ghost"
              size="icon-sm"
              className="md:hidden"
              onClick={() => setOpenMobile(false)}
              aria-label="Close sidebar"
            >
              <PanelLeftClose />
            </Button>
          </div>
          <Button
            variant="ghost"
            className="h-11 justify-start px-3"
            disabled={writesDisabled}
            onClick={(event) => {
              createTrigger.current = event.currentTarget;
              setActionError(null);
              setOpenMobile(false);
              changeCreateOpen(true);
            }}
          >
            <Plus />
            New agent
          </Button>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup className="px-3">
            <SidebarGroupLabel>Agents</SidebarGroupLabel>
            <SidebarMenu>
              {loading ? (
                [1, 2, 3].map((i) => (
                  <Skeleton key={i} className="my-1 h-10 w-full" />
                ))
              ) : agents.length === 0 ? (
                <p className="px-2 py-4 text-sm text-muted-foreground">
                  Your agents will appear here.
                </p>
              ) : (
                agents.map((agent) => (
                  <SidebarMenuItem key={agent.id}>
                    <SidebarMenuButton
                      isActive={active && selectedId === agent.id}
                      aria-pressed={active && selectedId === agent.id}
                      className="h-14 gap-3 pr-11"
                      onClick={() => selectAgent(agent.id)}
                      tooltip={agent.display_name}
                    >
                      <RuntimeIcon
                        kind={agent.runtime_kind}
                        className="size-5 shrink-0"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium">
                          {agent.display_name}
                        </span>
                        <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                          <span
                            aria-hidden="true"
                            className={cn(
                              "size-2 shrink-0 rounded-full",
                              ["ready", "running"].includes(
                                agent.observed_state,
                              )
                                ? "bg-success"
                                : ["failed", "error", "unknown"].includes(
                                      agent.observed_state,
                                    )
                                  ? "bg-warning"
                                  : "bg-muted-foreground",
                            )}
                          />
                          <span className="truncate capitalize">
                            {agent.observed_state.replaceAll("_", " ")}
                          </span>
                        </span>
                      </span>
                    </SidebarMenuButton>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          className="absolute right-1 top-1/2 -translate-y-1/2"
                          aria-label={`Actions for ${agent.display_name}`}
                          onClick={(event) => {
                            actionsTrigger.current = event.currentTarget;
                          }}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onSelect={() => openAgentSettings(agent.id)}
                        >
                          <Settings2 />
                          Agent settings
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          disabled={
                            writesDisabled ||
                            transitionalStates.has(agent.observed_state) ||
                            (agent.id === selectedId && operationActive) ||
                            ["ready", "running", "deleted"].includes(
                              agent.observed_state,
                            )
                          }
                          onSelect={() => {
                            selectAgent(agent.id);
                            lifecycle("start", false, agent);
                          }}
                        >
                          <Play />
                          Start agent
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={
                            writesDisabled ||
                            transitionalStates.has(agent.observed_state) ||
                            (agent.id === selectedId && operationActive) ||
                            ["stopped", "deleted"].includes(
                              agent.observed_state,
                            )
                          }
                          onSelect={() => {
                            selectAgent(agent.id);
                            lifecycle("stop", false, agent);
                          }}
                        >
                          <Square />
                          Stop agent
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          variant="destructive"
                          disabled={
                            writesDisabled ||
                            transitionalStates.has(agent.observed_state) ||
                            (agent.id === selectedId && operationActive) ||
                            agent.observed_state === "deleted"
                          }
                          onSelect={() => {
                            selectAgent(agent.id);
                            confirmationTrigger.current =
                              actionsTrigger.current;
                            setConfirmation({
                              action: "delete",
                              agentId: agent.id,
                            });
                          }}
                        >
                          <Trash2 />
                          Delete agent
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </SidebarMenuItem>
                ))
              )}
            </SidebarMenu>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter className="gap-3 p-3">
          <SidebarMenu>
            {[
              { id: "onboarding", label: "Employee setup", icon: ListChecks },
              { id: "usage", label: "Usage", icon: DollarSign },
              { id: "employees", label: "Employees", icon: Users },
              { id: "roles", label: "Roles", icon: ShieldCheck },
              { id: "setups", label: "Setups", icon: Package },
              { id: "settings", label: "Settings", icon: Settings },
              { id: "platform", label: "Platform status", icon: Activity },
            ].map(({ id, label, icon: Icon }) => (
              <SidebarMenuItem key={id}>
                <SidebarMenuButton
                  asChild
                  isActive={
                    page.split("?")[0] === id ||
                    (id === "setups" && page.startsWith("setups/"))
                  }
                  className="h-10"
                >
                  <a
                    href={`#${id}`}
                    onClick={navigate}
                    aria-current={
                      page.split("?")[0] === id ||
                      (id === "setups" && page.startsWith("setups/"))
                        ? "page"
                        : undefined
                    }
                  >
                    <Icon />
                    <span>{label}</span>
                  </a>
                </SidebarMenuButton>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
          <Separator />
          <Button variant="ghost" onClick={onSignOut} disabled={signingOut}>
            {signingOut ? "Signing out…" : "Sign out"}
          </Button>
          <div className="px-2 pb-1 text-xs text-muted-foreground">
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  connected ? "bg-success" : "bg-warning",
                )}
              />
              {connected ? "Local workspace" : "Local API unavailable"}
            </div>
            <p className="mt-1 pl-3.5">
              {version ? `Talos v${version}` : "Connecting…"}
            </p>
          </div>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset className="h-dvh min-w-0 overflow-hidden">
        <header className="flex h-16 shrink-0 items-center gap-3 px-4 sm:px-6">
          <SidebarTrigger ref={navigationTrigger} className="-ml-1 size-10" />
          {active ? (
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <h1 className="truncate text-base font-medium">
                  {selected?.display_name ?? "Talos"}
                </h1>
                {selected && <StateBadge state={selected.observed_state} />}
              </div>
              {selected && (
                <p className="truncate text-xs text-muted-foreground">
                  {runtimeName}
                </p>
              )}
            </div>
          ) : (
            <span className="text-sm font-medium">{pageTitle}</span>
          )}
        </header>
        <div
          id="main-content"
          tabIndex={-1}
          className="flex min-h-0 flex-1 flex-col outline-none"
        >
          <section
            hidden={!active || settingsActive}
            aria-label="Agent workspace"
            className="flex min-h-0 flex-1 flex-col"
          >
            {(actionError || pollError) && (
              <Alert className="mx-auto mb-3 w-[calc(100%-2rem)] max-w-3xl">
                <CircleAlert />
                <AlertDescription>
                  {actionError ?? pollError}
                  {retryRequest && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-2 w-fit"
                      disabled={submitting}
                      onClick={() => void mutate(retryRequest)}
                    >
                      Retry same request
                    </Button>
                  )}
                </AlertDescription>
              </Alert>
            )}
            {loading ? (
              <div className="mx-auto w-full max-w-3xl space-y-5 p-6">
                <Skeleton className="ml-auto h-12 w-2/3" />
                <Skeleton className="h-24 w-4/5" />
              </div>
            ) : !selected ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
                <Bot
                  className="size-9 text-muted-foreground"
                  strokeWidth={1.5}
                />
                <h2 className="text-2xl font-semibold tracking-tight">
                  Who will you work with?
                </h2>
                <p className="max-w-sm text-sm leading-relaxed text-muted-foreground">
                  Create an agent to start a conversation and give it a place to
                  work.
                </p>
                <Button
                  className="mt-2"
                  disabled={writesDisabled}
                  onClick={(event) => {
                    createTrigger.current = event.currentTarget;
                    setActionError(null);
                    changeCreateOpen(true);
                  }}
                >
                  <Plus />
                  New agent
                </Button>
              </div>
            ) : (
              <>
                <div
                  ref={conversation}
                  className="min-h-0 flex-1 overflow-y-auto"
                  role="log"
                  aria-label="Agent conversation"
                  aria-live="polite"
                >
                  <div
                    className={cn(
                      "mx-auto w-full max-w-3xl px-5 py-8 sm:px-8",
                      !selectedRuns.length &&
                        !historyLoading &&
                        "flex h-full flex-col items-center justify-center text-center",
                    )}
                  >
                    {historyLoading ? (
                      <div className="w-full space-y-5">
                        <Skeleton className="ml-auto h-12 w-2/3" />
                        <Skeleton className="h-24 w-4/5" />
                      </div>
                    ) : !selectedRuns.length ? (
                      <>
                        <MessageSquare
                          className="mb-5 size-8 text-muted-foreground"
                          strokeWidth={1.5}
                        />
                        <h2 className="text-2xl font-semibold tracking-tight">
                          What can {selected.display_name} help with?
                        </h2>
                        <p className="mt-3 max-w-md text-sm leading-relaxed text-muted-foreground">
                          {selected.runtime_mode === "native"
                            ? `Talk to ${runtimeName} using this agent’s saved conversation and applied permissions.`
                            : effectiveModel === "fixture"
                              ? "The local simulator is selected. No external provider is called."
                              : "Messages use this agent’s model configuration."}
                        </p>
                      </>
                    ) : (
                      [...selectedRuns].reverse().map((item) => (
                        <article
                          key={item.id}
                          className="mb-10 space-y-6 last:mb-0"
                        >
                          <div className="flex justify-end">
                            <div className="max-w-[85%] rounded-3xl bg-muted px-5 py-3">
                              <span className="sr-only">You: </span>
                              <p className="whitespace-pre-wrap break-words text-sm leading-7">
                                {item.message}
                              </p>
                            </div>
                          </div>
                          <div>
                            <p className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                              <RuntimeIcon
                                kind={selected.runtime_kind}
                                className="size-4"
                              />
                              {selected.display_name}
                            </p>
                            {(item.output || !item.error) && (
                              <p className="whitespace-pre-wrap break-words text-sm leading-7">
                                {item.output ||
                                  (activeRuns.has(item.status)
                                    ? "Waiting for a response…"
                                    : `Message ${item.status.replaceAll("_", " ")}.`)}
                              </p>
                            )}
                            {item.error && (
                              <p
                                role="status"
                                className="mt-2 whitespace-pre-wrap break-words text-sm text-destructive"
                              >
                                {item.error}
                              </p>
                            )}
                            {item.id === runId &&
                              runActive &&
                              item.status !== "unknown" &&
                              lastTool && (
                                <p
                                  role="status"
                                  className="mt-2 text-sm text-muted-foreground"
                                >
                                  {String(lastTool.payload.name)}:{" "}
                                  {String(lastTool.payload.phase)}
                                  {lastTool.payload.phase === "started"
                                    ? "…"
                                    : "; waiting for response…"}
                                </p>
                              )}
                          </div>
                        </article>
                      ))
                    )}
                    {selectedRun && (
                      <Collapsible className="mt-5 text-xs text-muted-foreground">
                        <CollapsibleTrigger asChild>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="-ml-2 text-muted-foreground"
                          >
                            Message details
                            <ChevronDown className="size-3" />
                          </Button>
                        </CollapsibleTrigger>
                        <CollapsibleContent className="space-y-3 pt-2">
                          <StateBadge state={selectedRun.status} />
                          <p className="break-words">
                            Model:{" "}
                            {selectedRun.model_id === "native"
                              ? `Configured in ${runtimeName}`
                              : selectedRun.model_id === "fixture"
                                ? "Local simulator"
                                : selectedRun.model_id}
                          </p>
                          <UsageDetails
                            calls={selectedRun.inference_calls ?? []}
                          />
                          {!["fixture", "native"].includes(
                            selectedRun.model_id,
                          ) && (
                            <p>
                              Run settings:{" "}
                              {Object.keys(
                                selectedRun.inference?.settings ?? {},
                              ).length
                                ? Object.entries(
                                    selectedRun.inference.settings!,
                                  )
                                    .map(
                                      ([key, value]) =>
                                        `${key.replaceAll("_", " ")}: ${value}`,
                                    )
                                    .join(" · ")
                                : "Model defaults"}
                            </p>
                          )}
                          {runEvents.length > 0 && (
                            <ol className="max-h-36 space-y-1 overflow-y-auto">
                              {runEvents.map((event) => (
                                <li key={event.sequence}>
                                  {event.sequence}.{" "}
                                  {event.type === "tool"
                                    ? `${event.payload.name}: ${event.payload.phase}`
                                    : event.type.replaceAll("_", " ")}
                                </li>
                              ))}
                            </ol>
                          )}
                        </CollapsibleContent>
                      </Collapsible>
                    )}
                  </div>
                </div>
                <div className="mx-auto w-full max-w-3xl shrink-0 px-4 pb-4 pt-2 sm:px-8">
                  {selectedRun?.status === "unknown" && (
                    <Alert className="mb-3">
                      <CircleAlert />
                      <AlertDescription>
                        Delivery could not be confirmed. Stop the agent, then
                        start it before sending another message. Talos will not
                        resend automatically.
                      </AlertDescription>
                    </Alert>
                  )}
                  {selected.last_error && (
                    <p role="alert" className="mb-3 text-sm text-danger">
                      {selected.last_error}
                    </p>
                  )}
                  {selectedOperation && (
                    <p
                      className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"
                      aria-live="polite"
                    >
                      {operationActive && (
                        <LoaderCircle className="size-3 animate-spin" />
                      )}
                      Latest operation:{" "}
                      {selectedOperation.status.replaceAll("_", " ")}
                      {selectedOperation.error &&
                      selectedOperation.error !== selected.last_error
                        ? `. ${selectedOperation.error}`
                        : ""}
                    </p>
                  )}
                  {active && !settingsActive && (
                    <div className="mb-4">
                      <AgentBudget employeeId={selected.employee_id} agentId={selected.id} coverage={budgetCoverage} />
                    </div>
                  )}
                  <form onSubmit={sendDiagnostic}>
                    <Label htmlFor="diagnostic-message" className="sr-only">
                      Message
                    </Label>
                    <InputGroup className="rounded-3xl border-border bg-muted/50 shadow-none">
                      <InputGroupTextarea
                        id="diagnostic-message"
                        rows={2}
                        maxLength={4000}
                        className="min-h-16 max-h-40 resize-none px-5 pt-4 text-sm"
                        placeholder={`Message ${selected.display_name}`}
                        value={message}
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [selectedId]: event.target.value,
                          }))
                        }
                        disabled={
                          writesDisabled ||
                          runActive ||
                          operationActive ||
                          historyLoading ||
                          !available
                        }
                        required
                        onKeyDown={(event) => {
                          if (
                            event.key === "Enter" &&
                            !event.shiftKey &&
                            !event.nativeEvent.isComposing
                          ) {
                            event.preventDefault();
                            event.currentTarget.form?.requestSubmit();
                          }
                        }}
                      />
                      <InputGroupAddon
                        align="block-end"
                        className="justify-between px-3 pb-3"
                      >
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="max-w-[70%] text-muted-foreground"
                          onClick={() => openAgentSettings(selected.id)}
                        >
                          <span className="truncate">
                            {selected.runtime_mode === "native"
                              ? (selected.inference_override?.model_id ??
                                runtimeName)
                              : effectiveModel === "fixture"
                                ? "Local simulator"
                                : (effectiveModel ?? "Model")}
                          </span>
                          <ChevronDown className="size-3" />
                        </Button>
                        {runActive && selectedRun?.status !== "unknown" ? (
                          <Button
                            type="button"
                            variant="secondary"
                            size="icon"
                            className="rounded-full"
                            aria-label={
                              selectedRun?.cancel_requested
                                ? "Cancelling response"
                                : "Cancel response"
                            }
                            disabled={
                              writesDisabled ||
                              selectedRun?.cancel_requested ||
                              selectedRun?.status === "cancel_requested"
                            }
                            onClick={() =>
                              void mutate({
                                path: `/runs/${runId}/cancel`,
                                method: "POST",
                                key: crypto.randomUUID(),
                                kind: "cancel",
                                agentId: selected.id,
                              })
                            }
                          >
                            <Square className="size-3.5 fill-current" />
                          </Button>
                        ) : (
                          <Button
                            type="submit"
                            size="icon"
                            className="rounded-full"
                            aria-label="Send message"
                            disabled={
                              writesDisabled ||
                              runActive ||
                              operationActive ||
                              historyLoading ||
                              !message.trim() ||
                              !available
                            }
                          >
                            <ArrowUp />
                          </Button>
                        )}
                      </InputGroupAddon>
                    </InputGroup>
                  </form>
                  <div className="mt-2 flex min-h-6 items-center justify-center gap-2 text-center text-xs text-muted-foreground">
                    {!available ? (
                      <>
                        <span>Start this agent to send a message.</span>
                        <Button
                          variant="link"
                          size="sm"
                          className="h-auto p-0 text-xs"
                          disabled={
                            writesDisabled ||
                            operationActive ||
                            selected.observed_state === "deleted"
                          }
                          onClick={() => lifecycle("start")}
                        >
                          Start agent
                        </Button>
                      </>
                    ) : (
                      <span>
                        {selected.runtime_mode === "managed" &&
                        selected.model_route === "fixture"
                          ? "Stop and start once to enable the model picker."
                          : "Conversations are saved in this workspace."}
                      </span>
                    )}
                  </div>
                </div>
              </>
            )}
          </section>
          <section
            hidden={!settingsActive}
            aria-label="Agent settings"
            className="min-h-0 flex-1 overflow-y-auto"
          >
            <div className="w-full max-w-3xl px-5 py-8 sm:px-8 sm:py-10">
              <Button variant="ghost" size="sm" className="-ml-3 mb-6" asChild>
                <a
                  href="#agents"
                  onClick={() => {
                    if (selected) setChatAgentId(selected.id);
                    navigate();
                  }}
                >
                  <ArrowLeft />
                  Back to chat
                </a>
              </Button>
              {selected ? (
                <>
                  <div className="mb-8">
                    <h2
                      id="agent-settings-heading"
                      tabIndex={-1}
                      className="text-2xl font-semibold tracking-tight"
                    >
                      Agent settings
                    </h2>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {selected.display_name} · {runtimeName}{" "}
                      {selected.runtime_release.slice(selected.runtime_kind.length + 1)} ·{" "}
                      {selected.employee_name || "Unassigned"}
                    </p>
                  </div>
                  <Tabs
                    value={agentView}
                    onValueChange={setAgentView}
                    className="pb-8"
                  >
                    <TabsList className="mb-7 w-full sm:w-auto">
                      <TabsTrigger value="settings">Settings</TabsTrigger>
                      <TabsTrigger value="permissions">Permissions</TabsTrigger>
                      <TabsTrigger value="handoff">Access & availability</TabsTrigger>
                    </TabsList>
                    <TabsContent value="settings">
                      {settingsActive && (
                        <div className="mb-5">
                          <AgentBudget employeeId={selected.employee_id} agentId={selected.id} coverage={budgetCoverage} />
                        </div>
                      )}
                      <a href={`#usage?agent_id=${selected.id}`} className="mb-5 block text-sm text-primary underline">View agent usage</a>
                      {selected.runtime_mode === "native" ? (
                        <div className="space-y-5">
                          <CaptureSetup
                            key={`capture-${selected.id}`}
                            agent={selected}
                            disabled={
                              writesDisabled || operationActive || runActive
                            }
                            operation={selectedOperation}
                            onOperation={onOperation}
                          />
                          <NativeModelSettings
                            key={selected.id}
                            agentId={selected.id}
                            modelId={
                              selected.inference_override?.model_id ?? null
                            }
                            disabled={writesDisabled || operationActive}
                            onOperation={onOperation}
                          />
                          <p className="text-xs text-muted-foreground">
                            {selected.inference_override
                              ? "Requests through Talos are tracked. Additional direct-provider traffic is outside Talos accounting."
                              : "Provider handled by agent: usage unavailable. Direct-provider traffic is outside Talos accounting."}
                          </p>
                          <div>
                            <h3 className="flex items-center gap-3 font-semibold">
                              <RuntimeIcon
                                kind={selected.runtime_kind}
                                className="size-8"
                              />
                              Your {runtimeName} workspace
                            </h3>
                            <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground">
                              Open {runtimeName} to manage native settings,
                              credentials and integrations. Your configuration
                              and files persist across stops and starts.
                            </p>
                          </div>
                          <Button
                            disabled={
                              writesDisabled ||
                              operationActive ||
                              selected.observed_state !== "ready"
                            }
                            onClick={openNativeWorkspace}
                          >
                            <ExternalLink aria-hidden="true" />
                            {selectedOperation?.action === "dashboard" &&
                            operationActive
                              ? `Opening ${runtimeName}…`
                              : `Open ${runtimeName}`}
                          </Button>
                          {selectedOperation?.dashboard_url && (
                            <p className="text-sm">
                              <a
                                href={selectedOperation.dashboard_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-primary underline underline-offset-4"
                              >
                                Continue if the new tab did not open
                              </a>
                            </p>
                          )}
                          <p className="max-w-xl text-sm leading-relaxed text-muted-foreground">
                            {selected.observed_state !== "ready"
                              ? "Start this agent to open its workspace."
                              : `First visit: ${selected.runtime_kind === "hermes" ? "sign in as talos with the dashboard password you chose, then " : ""}configure a provider in ${runtimeName} if you selected “Handled by agent”.`}
                          </p>
                          <p className="max-w-xl text-xs leading-relaxed text-muted-foreground">
                            Assigned agents use their applied role permissions.
                            Saving a role does not change running agents. Remote
                            Docker hosts require a tunnel for the workspace
                            port.
                          </p>
                        </div>
                      ) : (
                        <div>
                          <InferenceSettings
                            key={selected.id}
                            agentId={selected.id}
                            onSaved={(selection) =>
                              setAgents((current) =>
                                current.map((agent) =>
                                  agent.id === selected.id
                                    ? {
                                        ...agent,
                                        inference_override: selection.inherited
                                          ? null
                                          : selection,
                                      }
                                    : agent,
                                ),
                              )
                            }
                          />
                        </div>
                      )}
                    </TabsContent>
                    <TabsContent value="permissions" className="space-y-5">
                      {selected.runtime_mode === "native" && (
                        <SetupStatus agent={selected} />
                      )}
                      <div>
                        <Label
                          htmlFor="assigned-employee"
                          className="mb-2 block text-sm font-medium"
                        >
                          Employee assignment
                        </Label>
                        <Select
                          value={selected.employee_id ?? ""}
                          disabled={
                            writesDisabled ||
                            operationActive ||
                            selected.desired_state !== "stopped" ||
                            selected.observed_state !== "stopped"
                          }
                          onValueChange={(value) => void assignEmployee(value)}
                        >
                          <SelectTrigger
                            id="assigned-employee"
                            className="w-full"
                          >
                            <SelectValue placeholder="Choose an employee" />
                          </SelectTrigger>
                          <SelectContent>
                            {employees.map((employee) => (
                              <SelectItem key={employee.id} value={employee.id}>
                                {employee.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <p className="mt-2 text-xs text-muted-foreground">
                          Stop the agent before changing its assignment.{" "}
                          <a
                            href="#employees"
                            className="text-primary underline"
                          >
                            Manage employees
                          </a>
                        </p>
                      </div>
                      {selected.runtime_mode === "managed" ? (
                        <p className="text-sm text-muted-foreground">
                          Managed conversations have no native tools, regardless
                          of the assigned role.
                        </p>
                      ) : selected.role ? (
                        <>
                          <div className="flex flex-wrap items-center gap-3">
                            <h3 className="font-semibold">
                              {selected.role.name}
                            </h3>
                            <Badge
                              variant={
                                selected.permissions_pending
                                  ? "warning"
                                  : "success"
                              }
                            >
                              {selected.permissions_pending
                                ? "Changes pending"
                                : "Applied"}
                            </Badge>
                          </div>
                          <dl className="grid gap-5 sm:grid-cols-2">
                            <div>
                              <dt className="text-sm font-medium">
                                Saved permissions · revision{" "}
                                {selected.role.revision}
                              </dt>
                              <dd className="mt-2 text-sm text-muted-foreground">
                                {selected.role.capabilities
                                  .map(
                                    (id) =>
                                      catalog.find(
                                        (capability) => capability.id === id,
                                      )?.name ?? id,
                                  )
                                  .join(", ") || "No tools"}
                              </dd>
                            </div>
                            <div>
                              <dt className="text-sm font-medium">
                                Applied permissions
                                {selected.applied_role
                                  ? ` · ${selected.applied_role.name}, revision ${selected.applied_role.revision}`
                                  : ""}
                              </dt>
                              <dd className="mt-2 text-sm text-muted-foreground">
                                {selected.applied_role
                                  ? selected.applied_role.capabilities
                                      .map(
                                        (id) =>
                                          catalog.find(
                                            (capability) =>
                                              capability.id === id,
                                          )?.name ?? id,
                                      )
                                      .join(", ") || "No tools"
                                  : "Not applied yet"}
                              </dd>
                            </div>
                          </dl>
                          <p className="text-sm text-muted-foreground">
                            Apply interrupts running work and restarts the agent
                            if it was running. Starts preserve the selected
                            setup, permissions, and account versions.
                          </p>
                          <Button
                            variant="outline"
                            disabled={writesDisabled || operationActive}
                            onClick={() => lifecycle("apply-role")}
                          >
                            Apply saved role
                          </Button>
                          <p className="text-xs leading-relaxed text-muted-foreground">
                            Terminal execution permits file and network
                            operations even when dedicated tools are disabled.
                            Native settings are a trusted administrator surface;
                            direct edits there are outside role management.
                          </p>
                        </>
                      ) : (
                        <p className="text-sm text-muted-foreground">
                          This agent uses its existing native permissions.
                          Assign an employee to manage it through a role.
                        </p>
                      )}
                    </TabsContent>
                    <TabsContent value="handoff">
                      {settingsActive && <AgentHandoff key={selected.id} agent={selected} disabled={writesDisabled} />}
                    </TabsContent>
                  </Tabs>
                </>
              ) : loading ? (
                <Skeleton className="h-48 w-full" />
              ) : (
                <p className="text-sm text-muted-foreground">
                  This agent is no longer available.
                </p>
              )}
            </div>
          </section>
          <div hidden={active} className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-5xl px-5 py-8 sm:px-10 sm:py-10">
              {children}
              <p className="mt-10 text-xs leading-relaxed text-muted-foreground">
                Local administrator workspace. Employee sign-in is not enabled.
              </p>
            </div>
          </div>
        </div>
      </SidebarInset>
      <Dialog
        open={createOpen}
        onOpenChange={changeCreateOpen}
      >
        <DialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            (createTrigger.current?.isConnected
              ? createTrigger.current
              : navigationTrigger.current
            )?.focus();
          }}
          className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
          onEscapeKeyDown={(event) => {
            if (submitting) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (submitting) event.preventDefault();
          }}
          showCloseButton={!submitting}
        >
          <form onSubmit={createAgent} className="space-y-5">
            <DialogHeader>
              <DialogTitle>Create an agent</DialogTitle>
              <DialogDescription>
                Choose a name, runtime, and who this agent works for.
              </DialogDescription>
            </DialogHeader>
            <div>
              <Label
                htmlFor="agent-name"
                className="mb-1.5 block text-xs text-muted-foreground"
              >
                Name
              </Label>
              <Input
                id="agent-name"
                autoFocus
                className="w-full"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="Sales assistant"
                maxLength={120}
                required
                disabled={writesDisabled}
              />
            </div>
            <div>
              <Label
                htmlFor="create-employee"
                className="mb-1.5 block text-xs text-muted-foreground"
              >
                Employee
              </Label>
              <Select
                value={employeeId || "unassigned"}
                onValueChange={(value) =>
                  setEmployeeId(value === "unassigned" ? "" : value)
                }
                disabled={writesDisabled}
              >
                <SelectTrigger id="create-employee" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="unassigned">
                    Unassigned — use a label
                  </SelectItem>
                  {employees.map((employee) => (
                    <SelectItem key={employee.id} value={employee.id}>
                      {employee.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <a
                href="#employees"
                onClick={() => setCreateOpen(false)}
                className="mt-2 block text-xs text-primary underline"
              >
                Manage employees
              </a>
            </div>
            {!employeeId && (
              <div>
                <Label
                  htmlFor="employee-label"
                  className="mb-1.5 block text-xs text-muted-foreground"
                >
                  Employee label
                </Label>
                <Input
                  id="employee-label"
                  className="w-full"
                  value={employeeLabel}
                  onChange={(event) => setEmployeeLabel(event.target.value)}
                  placeholder="Alex"
                  required
                  minLength={1}
                  maxLength={160}
                  disabled={writesDisabled}
                />
              </div>
            )}
            <fieldset disabled={writesDisabled}>
              <legend className="mb-2 text-sm font-medium">
                Agent runtime
              </legend>
              <RadioGroup
                value={runtimeKind}
                onValueChange={(value) => {
                  setRuntimeKind(value as RuntimeKind);
                  setRuntimeVersion("latest");
                  setRuntimeMode("native");
                  setDashboardPassword("");
                }}
                disabled={writesDisabled}
                className="grid grid-cols-1 gap-3 min-[380px]:grid-cols-2"
              >
                {(Object.entries(runtimes) as [RuntimeKind, string][]).map(
                  ([kind, name]) => (
                    <Label
                      key={kind}
                      className="flex cursor-pointer items-center gap-2 rounded-lg border p-3 has-[[data-state=checked]]:border-foreground"
                    >
                      <RadioGroupItem value={kind} />
                      <RuntimeIcon kind={kind} className="size-5" />
                      {name}
                    </Label>
                  ),
                )}
              </RadioGroup>
              <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                {runtimeMode === "managed"
                  ? "OpenClaw with model settings and conversations managed by Talos."
                  : `Your own ${runtimes[runtimeKind]} workspace with the employee’s native tool permissions.`}
              </p>
            </fieldset>
            {runtimeMode === "native" && (
              <div>
                <Label
                  htmlFor="runtime-version"
                  className="mb-1.5 block text-xs text-muted-foreground"
                >
                  Version
                </Label>
                <Select
                  value={runtimeVersion}
                  onValueChange={setRuntimeVersion}
                  disabled={writesDisabled || !runtimeChoices.length}
                >
                  <SelectTrigger id="runtime-version" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="latest">
                      Latest supported
                      {runtimeChoices[0]
                        ? ` · ${runtimeChoices[0].runtime_release.slice(runtimeKind.length + 1)}`
                        : ""}
                    </SelectItem>
                    {runtimeChoices.map((target) => {
                      const version = target.runtime_release.slice(
                        runtimeKind.length + 1,
                      );
                      return (
                        <SelectItem key={version} value={version}>
                          {version}
                        </SelectItem>
                      );
                    })}
                  </SelectContent>
                </Select>
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  Latest selects the newest version supported by this Talos
                  installation. This agent keeps its version when restarted.
                </p>
                {runtimeTargetsError && (
                  <p role="alert" className="mt-2 text-sm text-danger">
                    Versions unavailable: {runtimeTargetsError}. Reopen this
                    dialog to retry.
                  </p>
                )}
              </div>
            )}
            {runtimeKind === "openclaw" ? (
              <Label className="flex items-start gap-3 text-sm">
                <Checkbox
                  checked={runtimeMode === "managed"}
                  onCheckedChange={(checked) =>
                    setRuntimeMode(checked === true ? "managed" : "native")
                  }
                  disabled={writesDisabled}
                  className="mt-0.5"
                />
                <span>
                  Use Talos-managed conversations
                  <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
                    Use Talos model settings and saved conversations. Native
                    tools are disabled in this mode.
                  </span>
                </span>
              </Label>
            ) : (
              <div>
                <Label
                  htmlFor="dashboard-password"
                  className="mb-1.5 block text-xs text-muted-foreground"
                >
                  Hermes dashboard password
                </Label>
                <Input
                  id="dashboard-password"
                  type="password"
                  autoComplete="new-password"
                  className="w-full"
                  value={dashboardPassword}
                  onChange={(event) => setDashboardPassword(event.target.value)}
                  minLength={12}
                  maxLength={256}
                  required
                  disabled={writesDisabled}
                  aria-describedby="dashboard-password-help"
                />
                <p
                  id="dashboard-password-help"
                  className="mt-2 text-xs leading-relaxed text-muted-foreground"
                >
                  At least 12 characters. Sign in to Hermes as{" "}
                  <span className="font-medium text-foreground">talos</span>{" "}
                  with this password. Keep it in your password manager.
                </p>
              </div>
            )}
            {runtimeMode === "native" ? (
              <NativeModelChoice
                active={createOpen}
                onConfigure={() => setCreateOpen(false)}
                value={createModel}
                onChange={setCreateModel}
                disabled={writesDisabled}
              />
            ) : (
              <p className="text-xs text-muted-foreground">
                Provider:{" "}
                {modelId === "fixture" ? "Local simulator" : "OpenRouter"} ·
                Model: {modelId ?? "Loading…"} (workspace default)
              </p>
            )}
            {(actionError || pollError) && (
              <div role="alert" className="space-y-3 text-sm text-danger">
                <p>{actionError ?? pollError}</p>
                {retryRequest?.kind === "create" && (
                  <Button
                    type="button"
                    variant="outline"
                    disabled={submitting}
                    onClick={() => void mutate(retryRequest)}
                  >
                    Retry same request
                  </Button>
                )}
              </div>
            )}
            <div className="flex justify-end gap-3 pt-2">
              <Button
                type="button"
                variant="outline"
                disabled={submitting}
                onClick={() => setCreateOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  writesDisabled ||
                  (runtimeMode === "native" &&
                    (!runtimeChoices.length || !!runtimeTargetsError)) ||
                  !displayName.trim() ||
                  (!employeeId && !employeeLabel.trim()) ||
                  (runtimeKind === "hermes" && dashboardPassword.length < 12)
                }
              >
                {submitting ? (
                  <LoaderCircle className="animate-spin" aria-hidden="true" />
                ) : (
                  <Plus aria-hidden="true" />
                )}
                {submitting ? "Creating…" : "Create agent"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      {confirmation && (
        <AlertDialog
          open
          onOpenChange={(open) => {
            if (!open) setConfirmation(null);
          }}
        >
          <AlertDialogContent
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              (confirmationTrigger.current?.isConnected
                ? confirmationTrigger.current
                : (actionsTrigger.current ?? navigationTrigger.current)
              )?.focus();
            }}
          >
            <AlertDialogHeader>
              <AlertDialogTitle>
                {confirmation.action === "delete"
                  ? `Delete ${agents.find((agent) => agent.id === confirmation.agentId)?.display_name ?? "agent"}?`
                  : "Apply saved role?"}
              </AlertDialogTitle>
              <AlertDialogDescription>
                {confirmation.action === "delete"
                  ? "This removes its runtime and private agent state. This cannot be undone."
                  : agents.find((agent) => agent.id === confirmation.agentId)
                        ?.desired_state === "running"
                    ? "Running work will be interrupted. This agent will restart with the selected setup, permissions, and account versions."
                    : "Apply the saved setup, permissions, and account versions. This agent will remain stopped."}
              </AlertDialogDescription>
            </AlertDialogHeader>
            {confirmation.action === "apply-role" && (
              <ApplyPreview
                loading={previewLoading}
                previews={
                  applyPreview
                    ? [
                        {
                          name:
                            agents.find(
                              (agent) => agent.id === confirmation.agentId,
                            )?.display_name ?? "Agent",
                          preview: applyPreview,
                        },
                      ]
                    : []
                }
              />
            )}
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                disabled={
                  confirmation.action === "apply-role" &&
                  (previewLoading ||
                    !applyPreview ||
                    !!applyPreview.blockers.length)
                }
                variant={
                  confirmation.action === "delete" ? "destructive" : "default"
                }
                onClick={() => {
                  lifecycle(
                    confirmation.action,
                    true,
                    agents.find((agent) => agent.id === confirmation.agentId),
                  );
                }}
              >
                {confirmation.action === "delete"
                  ? "Delete agent"
                  : "Apply saved role"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </>
  );
}
