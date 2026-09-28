import { useCallback, useEffect, useRef, useState } from "react";
import {
  Boxes,
  Check,
  CircleAlert,
  Database,
  Network,
  RefreshCw,
  Server,
} from "lucide-react";
import { SidebarProvider } from "@/components/ui/sidebar";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  InferenceSettings,
  OpenRouterSettings,
} from "@/components/inference-settings";
import { Setups } from "@/components/setups";
import { Connections } from "@/components/connections";
import { Administration } from "@/components/administration";
import { Usage } from "@/components/usage";
import { AgentWorkspace } from "@/components/agent-workspace";

type Status = {
  status: string;
  database: string;
  worker: string;
  gateway: string;
  version: string;
};

const services = [
  { key: "status", name: "Control plane", detail: "Local API", icon: Server },
  {
    key: "database",
    name: "Database",
    detail: "Persistent platform state",
    icon: Database,
  },
  {
    key: "worker",
    name: "Worker",
    detail: "Agent lifecycle management",
    icon: Boxes,
  },
  {
    key: "gateway",
    name: "Gateway",
    detail: "Agent access boundary",
    icon: Network,
  },
] as const;

function ServiceStatus({ value }: { value?: string }) {
  const healthy = value === "ok" || value === "ready";
  const label =
    value === "scaffold"
      ? "Scaffold only"
      : (value?.replaceAll("_", " ") ?? "Unknown");
  return (
    <Badge
      variant={
        healthy
          ? "success"
          : ["scaffold", "configured"].includes(value ?? "") || !value
            ? "secondary"
            : "warning"
      }
      className="capitalize"
    >
      {healthy && <Check className="size-3" aria-hidden="true" />}
      {label}
    </Badge>
  );
}

function readIds(key: string): Record<string, string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) ?? "{}");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return Object.fromEntries(
        Object.entries(value).filter(([, id]) => typeof id === "string"),
      );
    }
  } catch {
    /* Stored identifiers are optional; the server owns all records. */
  }
  return {};
}

function saveIds(key: string, ids: Record<string, string>) {
  try {
    localStorage.setItem(key, JSON.stringify(ids));
  } catch {
    /* Continue when local storage is disabled. */
  }
}

function pageFromHash(hash: string) {
  if (/^#setups(?:\/[^/]+)?$/.test(hash)) return hash.slice(1);
  if (hash === "#usage" || hash.startsWith("#usage?")) return hash.slice(1);
  if (/^#agents\/[^/]+\/settings$/.test(hash)) return hash.slice(1);
  if (["#platform", "#settings", "#roles", "#employees"].includes(hash))
    return hash.slice(1);
  if (hash === "" || hash === "#agents") return "agents";
  return null;
}

export default function App() {
  const [operationIds, setOperationIds] = useState(() =>
    readIds("talos.operationIds"),
  );
  const recordOperation = useCallback((agentId: string, id: string) => {
    setOperationIds((current) => ({ ...current, [agentId]: id }));
  }, []);
  useEffect(() => {
    saveIds("talos.operationIds", operationIds);
  }, [operationIds]);
  const [page, setPage] = useState(
    () => pageFromHash(window.location.hash) ?? "agents",
  );

  useEffect(() => {
    const navigate = () => {
      const nextPage = pageFromHash(window.location.hash);
      if (nextPage) setPage(nextPage);
    };
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
  const request = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/v1/status", {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok)
        throw new Error(`The status request returned HTTP ${response.status}.`);
      const data: unknown = await response.json();
      if (
        !data ||
        typeof data !== "object" ||
        !["status", "database", "worker", "gateway", "version"].every(
          (key) => typeof (data as Record<string, unknown>)[key] === "string",
        )
      ) {
        throw new Error("The API returned an unexpected status response.");
      }
      if (request.current !== controller) return;
      setStatus(data as Status);
      setError(null);
      setCheckedAt(new Date());
    } catch (cause) {
      if (request.current !== controller) return;
      setStatus(null);
      setError(
        controller.signal.aborted
          ? "The API did not respond in time. Check that the local stack is running."
          : cause instanceof Error
            ? cause.message
            : "Unable to reach the local API.",
      );
    } finally {
      window.clearTimeout(timeout);
      if (request.current === controller) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => void refresh(), 15_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
      request.current?.abort();
      request.current = null;
    };
  }, [refresh]);

  return (
    <SidebarProvider defaultOpen>
      <AgentWorkspace
        page={page}
        connected={status?.status === "ok"}
        version={status?.version}
        operationIds={operationIds}
        onOperation={recordOperation}
      >
        <div hidden={page !== "roles"}>
          <Administration
            kind="roles"
            active={page === "roles"}
            onOperation={recordOperation}
          />
        </div>
        <div hidden={page !== "employees"}>
          <Administration
            kind="employees"
            active={page === "employees"}
            onOperation={recordOperation}
          />
        </div>
        {(page === "setups" || page.startsWith("setups/")) && (
          <Setups key={page} setupId={page.split("/")[1]} />
        )}
        {page.startsWith("usage") && <Usage key={page} query={page.split("?")[1] ?? ""} />}
        {page === "settings" && (
          <section
            aria-labelledby="settings-heading"
            className="mx-auto max-w-2xl"
          >
            <div className="page-heading">
              <div>
                <h1 id="settings-heading">Settings</h1>
                <p>Provider credentials and defaults for your agents.</p>
              </div>
            </div>
            <OpenRouterSettings />
            <InferenceSettings />
            <Connections />
          </section>
        )}
        <section
          hidden={page !== "platform"}
          aria-labelledby="status-heading"
          className="mx-auto max-w-2xl"
        >
          <div className="page-heading">
            <div>
              <h1 id="status-heading">Platform status</h1>
              <p>The services behind your workspace.</p>
            </div>
            <Button
              variant="outline"
              onClick={() => {
                setLoading(true);
                void refresh();
              }}
              disabled={loading}
            >
              <RefreshCw
                aria-hidden="true"
                className={loading ? "animate-spin" : ""}
              />
              {loading ? "Checking" : "Refresh"}
            </Button>
          </div>
          {error && (
            <Alert className="mb-5">
              <CircleAlert />
              <AlertDescription>
                {error} Start or inspect the local services, then refresh.
              </AlertDescription>
            </Alert>
          )}
          <dl className="divide-y border-y">
            {services.map(({ key, name, detail, icon: Icon }) => (
              <div key={key} className="flex items-center gap-4 py-5">
                <Icon
                  className="size-5 text-muted-foreground"
                  aria-hidden="true"
                />
                <div className="min-w-0 flex-1">
                  <dt className="text-sm font-medium">{name}</dt>
                  <dd className="mt-1 text-sm text-muted-foreground">
                    {detail}
                  </dd>
                </div>
                <dd>
                  {loading && !status ? (
                    <Skeleton className="h-6 w-20" />
                  ) : (
                    <ServiceStatus value={status?.[key]} />
                  )}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-5 text-sm text-muted-foreground">
            Worker and gateway labels show configured capabilities, not live
            health.
          </p>
          <p className="mt-2 text-xs text-muted-foreground" aria-live="polite">
            {loading
              ? "Checking local services…"
              : error
                ? "Status unavailable. Retrying every 15 seconds."
                : `Last checked ${checkedAt?.toLocaleTimeString()}. Refreshes every 15 seconds.`}
          </p>
        </section>
      </AgentWorkspace>
    </SidebarProvider>
  );
}
