import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, Boxes, Check, CircleAlert, Database, Network, RefreshCw, Server, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  { key: "database", name: "Database", detail: "Persistent platform state", icon: Database },
  { key: "worker", name: "Worker", detail: "Agent lifecycle management", icon: Boxes },
  { key: "gateway", name: "Gateway", detail: "Agent access boundary", icon: Network },
] as const;

function ServiceStatus({ value }: { value?: string }) {
  const healthy = value === "ok" || value === "ready";
  const label = value === "scaffold" ? "Scaffold only" : value?.replaceAll("_", " ") ?? "Unknown";
  return (
    <Badge variant={healthy ? "success" : value === "scaffold" || !value ? "secondary" : "warning"} className="capitalize">
      {healthy && <Check className="size-3" aria-hidden="true" />}
      {label}
    </Badge>
  );
}

export default function App() {
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
      const response = await fetch("/api/v1/status", { signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error(`The status request returned HTTP ${response.status}.`);
      const data: unknown = await response.json();
      if (!data || typeof data !== "object" || !["status", "database", "worker", "gateway", "version"].every((key) => typeof (data as Record<string, unknown>)[key] === "string")) {
        throw new Error("The API returned an unexpected status response.");
      }
      if (request.current !== controller) return;
      setStatus(data as Status);
      setError(null);
      setCheckedAt(new Date());
    } catch (cause) {
      if (request.current !== controller) return;
      setStatus(null);
      setError(controller.signal.aborted ? "The API did not respond in time. Check that the local stack is running." : cause instanceof Error ? cause.message : "Unable to reach the local API.");
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
    <div className="min-h-screen">
      <header className="border-b">
        <div className="mx-auto flex h-20 max-w-6xl items-center justify-between gap-4 px-6 sm:px-10">
          <a href="/" className="flex items-center gap-3 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Talos home">
            <img src="/talos.svg" width="34" height="34" alt="" />
            <span className="text-xl font-semibold tracking-tight">Talos</span>
          </a>
          <Badge variant="secondary"><span className="size-1.5 rounded-full bg-slate-400" />Local development</Badge>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-12 sm:px-10 sm:py-16">
        <div className="mb-10 flex flex-wrap items-end justify-between gap-6">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Your agent workspace.</h1>
            <p className="mt-3 max-w-xl text-base leading-relaxed text-muted-foreground">A place to run personal agents with company-controlled access.</p>
          </div>
          <Button variant="outline" onClick={() => { setLoading(true); void refresh(); }} disabled={loading}>
            <RefreshCw aria-hidden="true" className={loading ? "animate-spin" : ""} />
            {loading ? "Checking" : "Refresh status"}
          </Button>
        </div>

        <AgentWorkspace />

        <details className="overflow-hidden rounded-xl border">
          <summary className="cursor-pointer bg-muted/40 px-6 py-4 text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">Platform status{status ? ` · ${status.status}` : " · Unavailable"}</summary>
        <section aria-labelledby="status-heading">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/40 px-6 py-5">
            <h2 id="status-heading" className="font-semibold">Platform status</h2>
            <span className="text-xs text-muted-foreground">{status ? `Version ${status.version}` : "Waiting for the API"}</span>
          </div>
          {error && <div role="alert" className="flex gap-3 border-b bg-amber-50 px-6 py-4 text-sm text-amber-950"><CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" /><p>{error} Start or inspect the Compose services, then refresh.</p></div>}
          <dl className="divide-y px-6">
            {services.map(({ key, name, detail, icon: Icon }) => (
              <div key={key} className="flex items-center justify-between gap-4 py-5">
                <dt className="flex items-center gap-4">
                  <Icon className="size-5 shrink-0 text-muted-foreground" strokeWidth={1.5} aria-hidden="true" />
                  <div><span className="text-sm font-medium">{name}</span><p className="mt-0.5 text-xs text-muted-foreground">{detail}</p></div>
                </dt>
                <dd><ServiceStatus value={status?.[key]} /></dd>
              </div>
            ))}
          </dl>
          <div className="border-t px-6 py-4 text-xs text-muted-foreground" aria-live="polite">
            {loading ? "Checking the local services…" : error ? "Status unavailable. Retrying every 15 seconds." : `Last checked ${checkedAt?.toLocaleTimeString()}. Updates every 15 seconds.`}
          </div>
        </section>
        </details>

        <div className="mt-8 flex items-start gap-3 text-sm text-muted-foreground">
          <ShieldCheck className="mt-0.5 size-5 shrink-0" strokeWidth={1.5} aria-hidden="true" />
          <p className="max-w-2xl leading-relaxed">This is a local prototype using a fake model. Business permissions and employee accounts are not available yet. Keep it on your own machine.</p>
        </div>
        <footer className="mt-16 flex flex-wrap items-center justify-between gap-4 border-t pt-6 text-xs text-muted-foreground">
          <span>Talos foundation</span>
          <a href="/docs" className="inline-flex items-center gap-1.5 rounded-sm hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">API reference <ArrowUpRight className="size-3.5" aria-hidden="true" /></a>
        </footer>
      </main>
    </div>
  );
}
