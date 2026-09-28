import { EmployeeBudget } from "@/components/employee-budget";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Plus,
  Trash2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  api,
  ApiError,
  errorMessage,
  type Role,
  type Employee,
  type Capability,
  type AgentPermissions,
} from "@/lib/api";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";

const pending = new Set(["queued", "running", "retry_wait"]);
type Result = {
  agent_id: string;
  id?: string;
  key: string;
  status: string;
  error?: string | null;
};

export function Administration({
  kind,
  active,
  onOperation,
}: {
  kind: "roles" | "employees";
  active: boolean;
  onOperation: (agentId: string, id: string) => void;
}) {
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
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const editorTrigger = useRef<HTMLButtonElement | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [confirmation, setConfirmation] = useState<"apply" | "delete" | null>(
    null,
  );
  const isRole = kind === "roles";
  const records = isRole ? roles : employees;
  const associated = agents.filter((agent) =>
    isRole ? agent.role?.id === selectedId : agent.employee_id === selectedId,
  );
  const selectedAgentIds = checked.filter((id) =>
    associated.some(
      (agent) => agent.id === id && agent.runtime_mode === "native",
    ),
  );
  const applicationPending = results.some((result) =>
    pending.has(result.status),
  );

  useEffect(() => {
    if (!active && !applicationPending) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const options = {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(8000),
          ]),
        };
        const [nextRoles, nextEmployees, nextAgents, nextCatalog] =
          await Promise.all([
            api<Role[]>("/roles", options),
            api<Employee[]>("/employees", options),
            api<AgentPermissions[]>("/agents", options),
            api<Capability[]>("/capabilities", options),
          ]);
        const updates = await Promise.all(
          results
            .filter((result) => result.id && pending.has(result.status))
            .map(async (result) => ({
              ...result,
              ...(await api<Result>(`/operations/${result.id}`, options)),
            })),
        );
        if (controller.signal.aborted) return;
        setRoles(nextRoles);
        setEmployees(nextEmployees);
        setAgents(nextAgents);
        setCatalog(nextCatalog);
        if (updates.length)
          setResults((current) => {
            const changed = updates.some((update) =>
              current.some(
                (result) =>
                  result.id === update.id &&
                  (result.status !== update.status ||
                    result.error !== update.error),
              ),
            );
            return changed
              ? current.map(
                  (result) =>
                    updates.find((update) => update.id === result.id) ?? result,
                )
              : current;
          });
        setLoading(false);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setError(errorMessage(cause));
          setLoading(false);
        }
      } finally {
        if (!controller.signal.aborted)
          timer = window.setTimeout(
            () => void poll(),
            applicationPending ? 2000 : 5000,
          );
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [active, applicationPending, refresh, results]);

  function choose(record?: Role | Employee) {
    setEditorOpen(true);
    setSelectedId(record?.id ?? "");
    setName(record?.name ?? "");
    setDescription(
      record && "capabilities" in record ? (record.description ?? "") : "",
    );
    setCapabilities(
      record && "capabilities" in record ? record.capabilities : [],
    );
    setEmail(record && "email" in record ? (record.email ?? "") : "");
    setRoleId(record && "role_id" in record ? record.role_id : "");
    setChecked([]);
    setError(null);
    setNotice("");
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      const result = await api<Role | Employee>(
        `/${kind}${selectedId ? `/${selectedId}` : ""}`,
        {
          method: selectedId ? "PUT" : "POST",
          body: JSON.stringify(
            isRole
              ? { name, description, capabilities }
              : { name, email: email || null, role_id: roleId },
          ),
        },
      );
      choose(result);
      setRefresh((value) => value + 1);
      setNotice(
        isRole
          ? "Role saved. Apply it to agents, or start them, to use these permissions."
          : "Employee saved. Role changes take effect when each agent starts or applies its role.",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setConfirmation(null);
    setSaving(true);
    setError(null);
    try {
      await api(`/${kind}/${selectedId}`, { method: "DELETE" });
      choose();
      setEditorOpen(false);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  async function applyOne(agentId: string, key: string) {
    setResults((current) => [
      ...current.filter((result) => result.agent_id !== agentId),
      { agent_id: agentId, key, status: "submitting" },
    ]);
    try {
      const operation = await api<Result & { id: string }>(
        `/agents/${agentId}/apply-role`,
        {
          method: "POST",
          headers: { "Idempotency-Key": key },
          signal: AbortSignal.timeout(15000),
        },
      );
      setResults((current) =>
        current.map((result) =>
          result.agent_id === agentId ? { ...operation, key } : result,
        ),
      );
      onOperation(agentId, operation.id);
    } catch (cause) {
      const uncertain = !(cause instanceof ApiError) || cause.status >= 500;
      setResults((current) =>
        current.map((result) =>
          result.agent_id === agentId
            ? {
                ...result,
                status: uncertain ? "unknown" : "failed",
                error: errorMessage(cause),
              }
            : result,
        ),
      );
    }
  }

  function applySelected(confirmed = false) {
    if (
      !confirmed &&
      associated.some(
        (agent) =>
          selectedAgentIds.includes(agent.id) &&
          agent.desired_state === "running",
      )
    ) {
      confirmationTrigger.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      setConfirmation("apply");
      return;
    }
    setConfirmation(null);
    for (const id of selectedAgentIds) void applyOne(id, crypto.randomUUID());
  }

  return (
    <section
      aria-label={isRole ? "Role administration" : "Employee administration"}
    >
      <div className="page-heading">
        <div>
          <h1>{isRole ? "Roles" : "Employees"}</h1>
          <p>
            {isRole
              ? "Define what your agents can do."
              : "The people your agents work with."}
          </p>
        </div>
        <Button
          disabled={saving}
          onClick={(event) => {
            editorTrigger.current = event.currentTarget;
            choose();
          }}
        >
          <Plus />
          New {isRole ? "role" : "employee"}
        </Button>
      </div>
      {error && !editorOpen && (
        <Alert className="mb-5">
          <CircleAlert />
          <AlertDescription>
            {error}
            <Button
              variant="outline"
              size="sm"
              className="mt-2 w-fit"
              onClick={() => {
                setError(null);
                setRefresh((value) => value + 1);
              }}
            >
              Refresh
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {notice && !editorOpen && (
        <p role="status" className="mb-5 text-sm text-success">
          {notice}
        </p>
      )}
      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      ) : !records.length ? (
        <div className="py-16 text-center">
          <h2 className="font-medium">
            {isRole ? "No roles yet" : "No employees yet"}
          </h2>
          <p className="mx-auto mt-2 max-w-sm text-sm leading-relaxed text-muted-foreground">
            {isRole
              ? "Create a role and choose the tools its agents can use. New roles start with no tools enabled."
              : "Create a role first, then add employees and assign their agents."}
          </p>
          <Button
            variant="outline"
            className="mt-5"
            onClick={(event) => {
              editorTrigger.current = event.currentTarget;
              choose();
            }}
          >
            <Plus />
            Create {isRole ? "a role" : "an employee"}
          </Button>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>{isRole ? "Capabilities" : "Role"}</TableHead>
              <TableHead className="hidden sm:table-cell">
                {isRole ? "Revision" : "Email"}
              </TableHead>
              <TableHead className="w-10">
                <span className="sr-only">Edit</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.map((record) => (
              <TableRow key={record.id}>
                <TableCell>
                  <Button
                    variant="link"
                    className="h-auto max-w-full justify-start whitespace-normal p-0 text-left font-medium"
                    disabled={saving}
                    onClick={(event) => {
                      editorTrigger.current = event.currentTarget;
                      choose(record);
                    }}
                  >
                    {record.name}
                  </Button>
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {"capabilities" in record
                    ? `${record.capabilities.length} enabled`
                    : (roles.find((role) => role.id === record.role_id)?.name ??
                      "Unassigned")}
                </TableCell>
                <TableCell className="hidden text-muted-foreground sm:table-cell">
                  {"capabilities" in record
                    ? record.revision
                    : record.email || "—"}
                </TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={saving}
                    onClick={(event) => {
                      editorTrigger.current = event.currentTarget;
                      choose(record);
                    }}
                    aria-label={`Edit ${record.name}`}
                  >
                    <ChevronRight />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Sheet
        open={active && editorOpen}
        onOpenChange={(open) => {
          if (!saving) setEditorOpen(open);
        }}
      >
        <SheetContent
          onCloseAutoFocus={(event) => {
            if (editorTrigger.current?.isConnected) {
              event.preventDefault();
              editorTrigger.current.focus();
            }
          }}
          className="w-full overflow-y-auto sm:max-w-xl"
          onEscapeKeyDown={(event) => {
            if (saving) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (saving) event.preventDefault();
          }}
        >
          <SheetHeader>
            <SheetTitle>
              {selectedId
                ? `Edit ${name}`
                : `Create ${isRole ? "role" : "employee"}`}
            </SheetTitle>
            <SheetDescription>
              {isRole
                ? "Choose the capabilities agents inherit from this role."
                : "Assign a role, then attach agents to this employee."}
            </SheetDescription>
          </SheetHeader>
          <div className="px-5 pb-8">
            {error && (
              <Alert className="mb-5">
                <CircleAlert />
                <AlertDescription>
                  {error}
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2 w-fit"
                    onClick={() => {
                      setError(null);
                      setRefresh((value) => value + 1);
                    }}
                  >
                    Refresh
                  </Button>
                </AlertDescription>
              </Alert>
            )}
            {notice && (
              <p role="status" className="mb-5 text-sm text-success">
                {notice}
              </p>
            )}
            <form onSubmit={save} className="max-w-2xl space-y-5">
              <div>
                <Label htmlFor={`${kind}-name`} className="mb-2 block text-sm">
                  Name
                </Label>
                <Input
                  id={`${kind}-name`}
                  required
                  maxLength={isRole ? 120 : 160}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  className="w-full"
                  disabled={saving}
                  placeholder={isRole ? "Sales" : "Alex"}
                />
              </div>
              {isRole ? (
                <>
                  <div>
                    <Label
                      htmlFor="role-description"
                      className="mb-2 block text-sm"
                    >
                      Description{" "}
                      <span className="text-muted-foreground">(optional)</span>
                    </Label>
                    <Textarea
                      id="role-description"
                      rows={2}
                      maxLength={2000}
                      value={description}
                      onChange={(event) => setDescription(event.target.value)}
                      className="w-full"
                      disabled={saving}
                    />
                  </div>
                  <fieldset disabled={saving}>
                    <legend className="mb-1 font-medium">Capabilities</legend>
                    <p className="mb-3 text-sm text-muted-foreground">
                      An empty role allows text conversations with no native
                      tools.
                    </p>
                    {catalog.map((capability) => (
                      <div
                        key={capability.id}
                        className="border-b py-4 last:border-b-0"
                      >
                        <Label className="flex cursor-pointer items-start gap-3 font-normal leading-relaxed">
                          <Checkbox
                            checked={capabilities.includes(capability.id)}
                            disabled={saving}
                            onCheckedChange={(checked) =>
                              setCapabilities((current) =>
                                checked === true
                                  ? [...current, capability.id]
                                  : current.filter(
                                      (id) => id !== capability.id,
                                    ),
                              )
                            }
                            className="mt-1"
                          />
                          <span>
                            <span className="font-medium">
                              {capability.name}
                            </span>
                            <span className="mt-1 block text-sm text-muted-foreground">
                              {capability.description}
                            </span>
                          </span>
                        </Label>
                        <Collapsible className="ml-7 mt-2">
                          <CollapsibleTrigger asChild>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-7 px-0 text-xs text-muted-foreground"
                            >
                              Runtime tools
                              <ChevronDown className="size-3" />
                            </Button>
                          </CollapsibleTrigger>
                          <CollapsibleContent className="break-words pt-2 text-xs leading-relaxed text-muted-foreground">
                            OpenClaw: {capability.openclaw.join(", ")}
                            <br />
                            Hermes: {capability.hermes_tools.join(", ")}{" "}
                            (toolsets: {capability.hermes.join(", ")})
                          </CollapsibleContent>
                        </Collapsible>
                      </div>
                    ))}
                  </fieldset>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    Permissions control native tools, not arbitrary code. Native
                    settings are a trusted administrator surface; changes made
                    there are outside role management.
                  </p>
                </>
              ) : (
                <>
                  <div>
                    <Label
                      htmlFor="employee-email"
                      className="mb-2 block text-sm"
                    >
                      Email{" "}
                      <span className="text-muted-foreground">(optional)</span>
                    </Label>
                    <Input
                      id="employee-email"
                      type="email"
                      maxLength={254}
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      className="w-full"
                      disabled={saving}
                    />
                  </div>
                  <div>
                    <Label
                      htmlFor="employee-role"
                      className="mb-2 block text-sm"
                    >
                      Role
                    </Label>
                    <Select
                      required
                      value={roleId}
                      onValueChange={setRoleId}
                      disabled={saving}
                    >
                      <SelectTrigger id="employee-role" className="w-full">
                        <SelectValue placeholder="Choose a role" />
                      </SelectTrigger>
                      <SelectContent>
                        {roles.map((role) => (
                          <SelectItem key={role.id} value={role.id}>
                            {role.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {!roles.length && (
                      <a
                        href="#roles"
                        className="mt-2 block text-sm text-primary underline"
                      >
                        Create a role first
                      </a>
                    )}
                  </div>
                </>
              )}
              <div className="flex flex-wrap gap-3">
                <Button
                  type="submit"
                  disabled={
                    saving || loading || !name.trim() || (!isRole && !roleId)
                  }
                >
                  {saving ? "Saving…" : `Save ${isRole ? "role" : "employee"}`}
                </Button>
                {selectedId && (
                  <Button
                    type="button"
                    variant="outline"
                    className="ml-auto text-danger"
                    disabled={saving}
                    onClick={(event) => {
                      confirmationTrigger.current = event.currentTarget;
                      setConfirmation("delete");
                    }}
                  >
                    <Trash2 aria-hidden="true" />
                    Delete
                  </Button>
                )}
              </div>
            </form>
            {!isRole && selectedId && (
              <div className="mt-6 border-t pt-5">
                <EmployeeBudget key={selectedId} employeeId={selectedId} editable />
              </div>
            )}
            {!isRole && selectedId && <a className="mt-5 block text-sm text-primary underline" href={`#usage?employee_id=${selectedId}`} onClick={() => setEditorOpen(false)}>View employee usage</a>}
            {selectedId && (
              <div className="mt-8 border-t pt-6">
                <h3 className="font-semibold">Assigned agents</h3>
                <p className="mt-2 text-sm text-muted-foreground">
                  {isRole
                    ? "Apply the saved role to selected native agents. Running work will be interrupted."
                    : "Manage assignments and test each agent on the Agents page."}
                </p>
                {!associated.length ? (
                  <p className="mt-4 text-sm text-muted-foreground">
                    No agents assigned yet.{" "}
                    <a href="#agents" className="text-primary underline">
                      Go to Agents
                    </a>
                  </p>
                ) : (
                  <ul className="mt-3 divide-y">
                    {associated.map((agent) => {
                      const result = results.find(
                        (item) => item.agent_id === agent.id,
                      );
                      const busy =
                        !!result &&
                        (pending.has(result.status) ||
                          result.status === "submitting" ||
                          result.status === "unknown");
                      return (
                        <li key={agent.id} className="py-3">
                          <Label className="flex items-start gap-3">
                            {isRole && (
                              <Checkbox
                                className="mt-1"
                                checked={checked.includes(agent.id)}
                                disabled={
                                  busy || agent.runtime_mode !== "native"
                                }
                                onCheckedChange={(checked) =>
                                  setChecked((current) =>
                                    checked === true
                                      ? [...current, agent.id]
                                      : current.filter((id) => id !== agent.id),
                                  )
                                }
                              />
                            )}
                            <span className="min-w-0 flex-1">
                              <span className="block break-words text-sm font-medium">
                                {agent.display_name}{" "}
                                <span className="font-normal text-muted-foreground">
                                  · {agent.employee_name}
                                </span>
                              </span>
                              <span className="mt-1 block text-xs text-muted-foreground">
                                {agent.runtime_mode === "managed"
                                  ? "Managed conversations · no tools"
                                  : `Saved revision ${agent.role?.revision} · applied ${agent.applied_role?.revision ?? "never"}`}
                              </span>
                            </span>
                            <Badge
                              variant={
                                agent.permissions_pending
                                  ? "warning"
                                  : "secondary"
                              }
                            >
                              {agent.runtime_mode === "managed"
                                ? "No tools"
                                : agent.permissions_pending
                                  ? "Changes pending"
                                  : "Applied"}
                            </Badge>
                          </Label>
                          {result && (
                            <div className="mt-2 text-xs" role="status">
                              {result.status.replaceAll("_", " ")}
                              {result.error ? `: ${result.error}` : ""}
                              {result.status === "unknown" && (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() =>
                                    void applyOne(agent.id, result.key)
                                  }
                                >
                                  Retry same request
                                </Button>
                              )}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
                {isRole && !!associated.length && (
                  <Button
                    className="mt-4"
                    variant="outline"
                    disabled={
                      !selectedAgentIds.length ||
                      results.some(
                        (result) =>
                          selectedAgentIds.includes(result.agent_id) &&
                          (pending.has(result.status) ||
                            ["submitting", "unknown"].includes(result.status)),
                      )
                    }
                    onClick={() => applySelected()}
                  >
                    Apply saved role to {selectedAgentIds.length || "selected"}{" "}
                    agents
                  </Button>
                )}
              </div>
            )}
          </div>
        </SheetContent>
      </Sheet>
      <AlertDialog
        open={active && !!confirmation}
        onOpenChange={(open) => {
          if (!open) setConfirmation(null);
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            if (confirmationTrigger.current?.isConnected) {
              event.preventDefault();
              confirmationTrigger.current.focus();
            }
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmation === "delete"
                ? `Delete ${name}?`
                : "Apply saved role?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmation === "delete"
                ? `Referenced ${isRole ? "roles" : "employees"} cannot be deleted.`
                : "Running work on the selected agents will be interrupted. Agents that were running will restart with the captured permissions."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant={confirmation === "delete" ? "destructive" : "default"}
              onClick={() =>
                confirmation === "delete" ? void remove() : applySelected(true)
              }
            >
              {confirmation === "delete"
                ? "Delete"
                : "Apply to selected agents"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
