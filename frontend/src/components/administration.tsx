import { useEffect, useRef, useState, type FormEvent } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api, ApiError, errorMessage, type Role, type Employee, type Capability, type AgentPermissions } from "@/lib/api";
import { cn } from "@/lib/utils";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const pending = new Set(["queued", "running", "retry_wait"]);
type Result = { agent_id: string; id?: string; key: string; status: string; error?: string | null };

export function Administration({ kind, active }: { kind: "roles" | "employees"; active: boolean }) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [agents, setAgents] = useState<AgentPermissions[]>([]);
  const [catalog, setCatalog] = useState<Capability[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [email, setEmail] = useState("");
  const [roleId, setRoleId] = useState("");
  const [checked, setChecked] = useState<string[]>([]);
  const [results, setResults] = useState<Result[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const applyDialog = useRef<HTMLDialogElement>(null);
  const isRole = kind === "roles";
  const records = isRole ? roles : employees;
  const associated = agents.filter((agent) => isRole ? agent.role?.id === selectedId : agent.employee_id === selectedId);
  const selectedAgentIds = checked.filter((id) => associated.some((agent) => agent.id === id && agent.runtime_mode === "native"));
  const applicationPending = results.some((result) => pending.has(result.status));

  useEffect(() => {
    if (!active && !applicationPending) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const options = { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]) };
        const [nextRoles, nextEmployees, nextAgents, nextCatalog] = await Promise.all([
          api<Role[]>("/roles", options), api<Employee[]>("/employees", options),
          api<AgentPermissions[]>("/agents", options), api<Capability[]>("/capabilities", options),
        ]);
        const updates = await Promise.all(results.filter((result) => result.id && pending.has(result.status)).map(async (result) => ({ ...result, ...await api<Result>(`/operations/${result.id}`, options) })));
        if (controller.signal.aborted) return;
        setRoles(nextRoles); setEmployees(nextEmployees); setAgents(nextAgents); setCatalog(nextCatalog);
        if (updates.length) setResults((current) => {
          const changed = updates.some((update) => current.some((result) => result.id === update.id && (result.status !== update.status || result.error !== update.error)));
          return changed ? current.map((result) => updates.find((update) => update.id === result.id) ?? result) : current;
        });
        setLoading(false);
      } catch (cause) {
        if (!controller.signal.aborted) { setError(errorMessage(cause)); setLoading(false); }
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), applicationPending ? 2000 : 5000);
      }
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [active, applicationPending, refresh, results]);

  function choose(record?: Role | Employee) {
    setSelectedId(record?.id ?? ""); setName(record?.name ?? "");
    setDescription(record && "capabilities" in record ? record.description ?? "" : "");
    setCapabilities(record && "capabilities" in record ? record.capabilities : []);
    setEmail(record && "email" in record ? record.email ?? "" : "");
    setRoleId(record && "role_id" in record ? record.role_id : "");
    setChecked([]); setError(null); setNotice("");
  }

  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError(null); setNotice("");
    try {
      const result = await api<Role | Employee>(`/${kind}${selectedId ? `/${selectedId}` : ""}`, {
        method: selectedId ? "PUT" : "POST",
        body: JSON.stringify(isRole ? { name, description, capabilities } : { name, email: email || null, role_id: roleId }),
      });
      choose(result); setRefresh((value) => value + 1);
      setNotice(isRole ? "Role saved. Apply it to agents, or start them, to use these permissions." : "Employee saved. Role changes take effect when each agent starts or applies its role.");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setSaving(false); }
  }

  async function remove() {
    if (!window.confirm(`Delete ${name}? Referenced ${isRole ? "roles" : "employees"} cannot be deleted.`)) return;
    setSaving(true); setError(null);
    try { await api(`/${kind}/${selectedId}`, { method: "DELETE" }); choose(); setRefresh((value) => value + 1); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setSaving(false); }
  }

  async function applyOne(agentId: string, key: string) {
    setResults((current) => [...current.filter((result) => result.agent_id !== agentId), { agent_id: agentId, key, status: "submitting" }]);
    try {
      const operation = await api<Result>(`/agents/${agentId}/apply-role`, { method: "POST", headers: { "Idempotency-Key": key }, signal: AbortSignal.timeout(15000) });
      setResults((current) => current.map((result) => result.agent_id === agentId ? { ...operation, key } : result));
      // Reuse the existing per-agent operation view if the admin navigates away.
      try { const ids = JSON.parse(localStorage.getItem("talos.operationIds") ?? "{}"); localStorage.setItem("talos.operationIds", JSON.stringify({ ...ids, [agentId]: operation.id })); } catch { /* Server operations remain durable. */ }
    } catch (cause) {
      const uncertain = !(cause instanceof ApiError) || cause.status >= 500;
      setResults((current) => current.map((result) => result.agent_id === agentId ? { ...result, status: uncertain ? "unknown" : "failed", error: errorMessage(cause) } : result));
    }
  }

  function applySelected(confirmed = false) {
    if (!confirmed && associated.some((agent) => selectedAgentIds.includes(agent.id) && agent.desired_state === "running")) { applyDialog.current?.showModal(); return; }
    applyDialog.current?.close();
    for (const id of selectedAgentIds) void applyOne(id, crypto.randomUUID());
  }

  return <section aria-label={isRole ? "Role administration" : "Employee administration"}>
    <dialog ref={applyDialog} aria-labelledby={`${kind}-apply-title`} className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-xl bg-panel p-6 text-foreground shadow-xl backdrop:bg-black/65">
      <h2 id={`${kind}-apply-title`} className="text-lg font-semibold">Apply saved role?</h2><p className="my-4 text-sm text-muted-foreground">Running work on the selected agents will be interrupted. Agents that were running will restart with the captured permissions.</p><div className="flex justify-end gap-3"><Button variant="outline" onClick={() => applyDialog.current?.close()}>Cancel</Button><Button onClick={() => applySelected(true)}>Apply to selected agents</Button></div>
    </dialog>
    <div className="page-heading"><div><h1>{isRole ? "Roles" : "Employees"}</h1><p>{isRole ? "Choose the capabilities agents inherit from an employee’s role." : "Assign each employee a role, then attach their agents."}</p></div><Button disabled={saving} onClick={() => choose()}><Plus aria-hidden="true" />New {isRole ? "role" : "employee"}</Button></div>
    {error && <div role="alert" className="notice notice-warning mb-5">{error}<Button variant="outline" size="sm" onClick={() => { setError(null); setRefresh((value) => value + 1); }}>Refresh</Button></div>}
    {notice && <p role="status" className="mb-5 text-sm text-success">{notice}</p>}
    <div className="agent-layout">
      <aside className="agent-list-panel p-3" aria-label={isRole ? "Roles" : "Employees"}>
        {loading ? <p className="p-3 text-sm text-muted-foreground">Loading…</p> : records.length === 0 ? <p className="p-3 text-sm text-muted-foreground">{isRole ? "Create a role to get started. New roles have no tools enabled." : "No employees yet. Create a role first, then add your employees."}</p> : records.map((record) => <button key={record.id} type="button" disabled={saving} aria-pressed={selectedId === record.id} onClick={() => choose(record)} className={cn("mb-1 block w-full rounded-md px-3 py-3 text-left text-sm hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring", selectedId === record.id && "bg-primary/10 text-primary")}><span className="block break-words font-medium">{record.name}</span><span className="mt-1 block text-xs text-muted-foreground">{"capabilities" in record ? `${record.capabilities.length} capabilities · revision ${record.revision}` : roles.find((role) => role.id === record.role_id)?.name}</span></button>)}
      </aside>
      <div className="min-w-0 bg-panel p-6">
        <form onSubmit={save} className="max-w-2xl space-y-5">
          <h2 className="text-lg font-semibold">{selectedId ? "Edit" : "Create"} {isRole ? "role" : "employee"}</h2>
          <div><label htmlFor={`${kind}-name`} className="mb-2 block text-sm">Name</label><input id={`${kind}-name`} required maxLength={isRole ? 120 : 160} value={name} onChange={(event) => setName(event.target.value)} className={inputClass} disabled={saving} placeholder={isRole ? "Sales" : "Alex"} /></div>
          {isRole ? <>
            <div><label htmlFor="role-description" className="mb-2 block text-sm">Description <span className="text-muted-foreground">(optional)</span></label><textarea id="role-description" rows={2} maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} className={inputClass} disabled={saving} /></div>
            <fieldset disabled={saving}><legend className="mb-1 font-medium">Capabilities</legend><p className="mb-3 text-sm text-muted-foreground">An empty role allows text conversations with no native tools.</p>
              {catalog.map((capability) => <label key={capability.id} className="flex cursor-pointer items-start gap-3 border-b py-4 last:border-b-0"><input type="checkbox" checked={capabilities.includes(capability.id)} onChange={(event) => setCapabilities((current) => event.target.checked ? [...current, capability.id] : current.filter((id) => id !== capability.id))} className="mt-1 size-4 shrink-0 accent-primary" /><span className="min-w-0"><span className="font-medium">{capability.name}</span><span className="mt-1 block text-sm text-muted-foreground">{capability.description}</span><span className="mt-2 block break-words text-xs text-muted-foreground">OpenClaw: {capability.openclaw.join(", ")}<br />Hermes: {capability.hermes_tools.join(", ")} (toolsets: {capability.hermes.join(", ")})</span></span></label>)}
            </fieldset>
            <p className="text-xs leading-relaxed text-muted-foreground">Permissions control native tools, not arbitrary code. Native settings are a trusted administrator surface; changes made there are outside role management.</p>
          </> : <>
            <div><label htmlFor="employee-email" className="mb-2 block text-sm">Email <span className="text-muted-foreground">(optional)</span></label><input id="employee-email" type="email" maxLength={254} value={email} onChange={(event) => setEmail(event.target.value)} className={inputClass} disabled={saving} /></div>
            <div><label htmlFor="employee-role" className="mb-2 block text-sm">Role</label><select id="employee-role" required value={roleId} onChange={(event) => setRoleId(event.target.value)} className={inputClass} disabled={saving}><option value="">Choose a role</option>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select>{!roles.length && <a href="#roles" className="mt-2 block text-sm text-primary underline">Create a role first</a>}</div>
          </>}
          <div className="flex flex-wrap gap-3"><Button type="submit" disabled={saving || loading || !name.trim() || (!isRole && !roleId)}>{saving ? "Saving…" : `Save ${isRole ? "role" : "employee"}`}</Button>{selectedId && <Button type="button" variant="outline" className="ml-auto text-danger" disabled={saving} onClick={() => void remove()}><Trash2 aria-hidden="true" />Delete</Button>}</div>
        </form>
        {selectedId && <div className="mt-8 border-t pt-6"><h3 className="font-semibold">Assigned agents</h3><p className="mt-2 text-sm text-muted-foreground">{isRole ? "Apply the saved role to selected native agents. Running work will be interrupted." : "Manage assignments and test each agent on the Agents page."}</p>
          {!associated.length ? <p className="mt-4 text-sm text-muted-foreground">No agents assigned yet. <a href="#agents" className="text-primary underline">Go to Agents</a></p> : <ul className="mt-3 divide-y">{associated.map((agent) => { const result = results.find((item) => item.agent_id === agent.id); const busy = !!result && (pending.has(result.status) || result.status === "submitting" || result.status === "unknown"); return <li key={agent.id} className="py-3"><label className="flex items-start gap-3">{isRole && <input type="checkbox" className="mt-1 size-4 accent-primary" checked={checked.includes(agent.id)} disabled={busy || agent.runtime_mode !== "native"} onChange={(event) => setChecked((current) => event.target.checked ? [...current, agent.id] : current.filter((id) => id !== agent.id))} />}<span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{agent.display_name} <span className="font-normal text-muted-foreground">· {agent.employee_name}</span></span><span className="mt-1 block text-xs text-muted-foreground">{agent.runtime_mode === "managed" ? "Managed conversations · no tools" : `Saved revision ${agent.role?.revision} · applied ${agent.applied_role?.revision ?? "never"}`}</span></span><Badge variant={agent.permissions_pending ? "warning" : "secondary"}>{agent.runtime_mode === "managed" ? "No tools" : agent.permissions_pending ? "Changes pending" : "Applied"}</Badge></label>{result && <div className="mt-2 text-xs" role="status">{result.status.replaceAll("_", " ")}{result.error ? `: ${result.error}` : ""}{result.status === "unknown" && <Button variant="outline" size="sm" onClick={() => void applyOne(agent.id, result.key)}>Retry same request</Button>}</div>}</li>; })}</ul>}
          {isRole && !!associated.length && <Button className="mt-4" variant="outline" disabled={!selectedAgentIds.length || results.some((result) => selectedAgentIds.includes(result.agent_id) && (pending.has(result.status) || ["submitting", "unknown"].includes(result.status)))} onClick={() => applySelected()}>Apply saved role to {selectedAgentIds.length || "selected"} agents</Button>}
        </div>}
      </div>
    </div>
  </section>;
}
