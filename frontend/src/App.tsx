import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, Bot, Boxes, Check, ChevronRight, CircleAlert, Database, LayoutDashboard, Menu, Network, RefreshCw, Server, X } from "lucide-react";
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
    <Badge variant={healthy ? "success" : ["scaffold", "configured"].includes(value ?? "") || !value ? "secondary" : "warning"} className="capitalize">
      {healthy && <Check className="size-3" aria-hidden="true" />}
      {label}
    </Badge>
  );
}

export default function App() {
  const [page, setPage] = useState(() => window.location.hash === "#platform" ? "platform" : "agents");
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    const navigate = () => {
      if (["", "#platform", "#agents"].includes(window.location.hash)) {
        setPage(window.location.hash === "#platform" ? "platform" : "agents");
      }
      setMenuOpen(false);
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
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="app-sidebar" aria-label="Workspace navigation">
        <div className="brand-row">
          <a href="#agents" className="brand" aria-label="Talos agents" onClick={() => setMenuOpen(false)}>
            <img src="/talos.svg" width="29" height="29" alt="" />
            <span>Talos</span>
          </a>
          <button className="mobile-menu" type="button" aria-expanded={menuOpen} aria-controls="workspace-navigation" aria-label={menuOpen ? "Close navigation" : "Open navigation"} onClick={() => setMenuOpen(!menuOpen)}>
            {menuOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
        <div id="workspace-navigation" className={`sidebar-body ${menuOpen ? "is-open" : ""}`}>
          <div className="workspace-context">
            <span className="workspace-icon"><Server size={17} aria-hidden="true" /></span>
            <div><p>Local workspace</p><span>Single-host installation</span></div>
          </div>
          <nav className="main-navigation" aria-label="Main">
            <a href="#agents" aria-current={page === "agents" ? "page" : undefined} onClick={() => setMenuOpen(false)}><Bot size={18} aria-hidden="true" />Agents</a>
            <a href="#platform" aria-current={page === "platform" ? "page" : undefined} onClick={() => setMenuOpen(false)}><LayoutDashboard size={18} aria-hidden="true" />Platform status</a>
          </nav>
          <div className="sidebar-footer">
            <a href="/docs" className="reference-link">API reference<ArrowUpRight size={15} aria-hidden="true" /></a>
            <div className="installation-status"><span className={`status-dot ${status?.status === "ok" ? "is-healthy" : ""}`} /><span>{status?.status === "ok" ? "Local API connected" : "Local API unavailable"}</span></div>
            <span className="version-label">Talos {status ? `v${status.version}` : "foundation"}</span>
          </div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <nav aria-label="Breadcrumb" className="breadcrumb"><span>Local workspace</span><ChevronRight size={14} aria-hidden="true" /><span aria-current="page">{page === "agents" ? "Agents" : "Platform status"}</span></nav>
          <span className="local-label"><span className="status-dot" />Local development</span>
        </header>
        <main id="main-content" tabIndex={-1} className="page-content">
          <div hidden={page !== "agents"}>
            <AgentWorkspace />
          </div>
          <section hidden={page !== "platform"} aria-labelledby="status-heading">
            <div className="page-heading">
              <div><h1 id="status-heading">Platform status</h1><p>Check the services behind your agent workspace.</p></div>
              <Button variant="outline" onClick={() => { setLoading(true); void refresh(); }} disabled={loading}>
                <RefreshCw aria-hidden="true" className={loading ? "animate-spin" : ""} />{loading ? "Checking" : "Refresh status"}
              </Button>
            </div>
            {error && <div role="alert" className="notice notice-warning mb-5"><CircleAlert className="size-4 shrink-0" aria-hidden="true" /><p>{error} Start or inspect the local services, then refresh.</p></div>}
            <dl className="service-grid">
              {services.map(({ key, name, detail, icon: Icon }) => (
                <div key={key} className="service-card">
                  <dt><Icon size={19} strokeWidth={1.5} aria-hidden="true" /><span>{name}</span></dt>
                  <dd><ServiceStatus value={status?.[key]} /></dd>
                  <dd className="service-detail">{detail}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-5 text-sm text-muted-foreground">Worker and gateway labels show configured capabilities, not live health.</p>
            <p className="mt-2 text-xs text-muted-foreground" aria-live="polite">{loading ? "Checking local services…" : error ? "Status unavailable. Retrying every 15 seconds." : `Last checked ${checkedAt?.toLocaleTimeString()}. Refreshes every 15 seconds.`}</p>
          </section>
          <footer className="page-footer">Local prototype. Employee accounts and business permissions are not available yet.</footer>
        </main>
      </div>
    </div>
  );
}
