import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { BudgetStatus } from "@/components/employee-budget";
import { formatUsd as usd } from "@/lib/utils";
import { api, errorMessage, type Budget } from "@/lib/api";

export type UsageTotals = {
  calls: number;
  known_spend_usd: string;
  reported_cost_calls: number;
  unresolved_calls: number;
  missing_cost_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
};
type Option = {
  id: string;
  name: string;
  deleted?: boolean;
  coverage?: string;
};
type Summary = {
  period: { start: string; end: string; timezone: string };
  tracking_started_at: string;
  history_status: string;
  coverage: string;
  total: UsageTotals;
  budgets: Budget[];
  employees: (UsageTotals & { id: string | null })[];
  agents: (UsageTotals & { id: string })[];
  options: { employees: Option[]; agents: Option[] };
};
type Call = {
  id: string;
  agent_id: string;
  employee_id: string | null;
  model: string;
  generation_id: string | null;
  admitted_at: string;
  completed_at: string | null;
  outcome: string;
  cost_usd: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
  duration_ms: number | null;
};
type Calls = { items: Call[]; next_cursor: string | null };
const label = (options: Option[], id: string | null) =>
  id === null
    ? "Unassigned"
    : (options.find((option) => option.id === id)?.name ?? id);

export function Usage({ query }: { query: string }) {
  const params = new URLSearchParams(query);
  const month = params.get("month") || new Date().toISOString().slice(0, 7);
  const employee = params.get("employee_id") || "";
  const agent = params.get("agent_id") || "";
  const [summary, setSummary] = useState<Summary | null>(null);
  const [calls, setCalls] = useState<Calls | null>(null);
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors[cursors.length - 1];
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const filters = new URLSearchParams({ month });
        if (employee) filters.set("employee_id", employee);
        if (agent) filters.set("agent_id", agent);
        const options = {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(10000),
          ]),
        };
        const [nextSummary, nextCalls] = await Promise.all([
          api<Summary>(`/usage?${filters}`, options),
          api<Calls>(
            `/usage/calls?${filters}&limit=25&cursor=${encodeURIComponent(cursor)}`,
            options,
          ),
        ]);
        if (!controller.signal.aborted) {
          setSummary(nextSummary);
          setCalls(nextCalls);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = window.setTimeout(() => void load(), 15000);
        }
      }
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [month, employee, agent, cursor, refresh]);

  function filter(key: string, value: string) {
    const next = new URLSearchParams(query);
    if (value && value !== "all") next.set(key, value);
    else next.delete(key);
    window.location.assign(`#usage?${next}`);
  }
  function breakdown(
    title: string,
    rows: (UsageTotals & { id: string | null })[],
    options: Option[],
    key: string,
  ) {
    return (
      <section className="min-w-0" aria-label={title}>
        <h2 className="mb-3 font-semibold">{title}</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Known spend</TableHead>
              <TableHead>Calls</TableHead>
              <TableHead>Incomplete</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.id ?? "unassigned"}>
                <TableCell>
                  {row.id ? (
                    <button
                      className="text-left underline underline-offset-4"
                      onClick={() => filter(key, row.id!)}
                    >
                      {label(options, row.id)}
                    </button>
                  ) : (
                    "Unassigned"
                  )}
                </TableCell>
                <TableCell>{usd(row.known_spend_usd)}</TableCell>
                <TableCell>{row.calls}</TableCell>
                <TableCell>
                  {row.missing_cost_calls + row.unresolved_calls}
                </TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={4}>
                  No tracked calls in this period.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </section>
    );
  }

  return (
    <section
      className="mx-auto max-w-6xl space-y-6"
      aria-labelledby="usage-heading"
    >
      <div className="page-heading">
        <div>
          <h1 id="usage-heading">Usage</h1>
          <p>Provider spending recorded by Talos, in USD.</p>
        </div>
        <Button
          variant="outline"
          disabled={loading}
          onClick={() => {
            setLoading(true);
            setRefresh((v) => v + 1);
          }}
        >
          Refresh
        </Button>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor="usage-month">Month (UTC)</Label>
          <Input
            id="usage-month"
            type="month"
            value={month}
            onChange={(e) => filter("month", e.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="usage-employee">Employee</Label>
          <Select
            value={employee || "all"}
            onValueChange={(v) => filter("employee_id", v)}
          >
            <SelectTrigger id="usage-employee" className="w-full">
              <SelectValue placeholder="All employees" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All employees</SelectItem>
              {summary?.options.employees.map((e) => (
                <SelectItem key={e.id} value={e.id}>
                  {e.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="usage-agent">Agent</Label>
          <Select
            value={agent || "all"}
            onValueChange={(v) => filter("agent_id", v)}
          >
            <SelectTrigger id="usage-agent" className="w-full">
              <SelectValue placeholder="All agents" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All agents</SelectItem>
              {summary?.options.agents.map((a) => (
                <SelectItem key={a.id} value={a.id}>
                  {a.name}
                  {a.deleted ? " (deleted)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      {error && (
        <Alert role="alert">
          <AlertDescription>
            {error} Displayed data may be out of date.
          </AlertDescription>
        </Alert>
      )}
      {!summary && loading && <Skeleton className="h-32 w-full" />}
      {summary && (
        <>
          <p className="text-sm text-muted-foreground">{summary.coverage}</p>
          {agent && (
            <p className="text-sm text-muted-foreground">
              {summary.options.agents.find((a) => a.id === agent)?.coverage}
            </p>
          )}
          {summary.history_status !== "tracked" && (
            <Alert>
              <AlertDescription>
                {summary.history_status === "before_tracking"
                  ? "This period predates durable tracking. Historical costs are unavailable."
                  : "This is a partial tracking period."}{" "}
                Tracking began{" "}
                {new Date(summary.tracking_started_at).toLocaleString("en-GB", {
                  timeZone: "UTC",
                })}{" "}
                UTC. Older run reports are not included.
              </AlertDescription>
            </Alert>
          )}
          {(summary.total.missing_cost_calls > 0 ||
            summary.total.unresolved_calls > 0) && (
            <Alert role="status">
              <AlertDescription>
                Accounting is incomplete: {summary.total.missing_cost_calls}{" "}
                finished calls have no reported cost and{" "}
                {summary.total.unresolved_calls} calls are unresolved (possibly
                still running). Known spend excludes unknown charges.
              </AlertDescription>
            </Alert>
          )}
          {summary.budgets.length > 0 && (
            <section
              aria-label="Current employee allowances"
              className="space-y-4"
            >
              <h2 className="font-semibold">Current employee allowances</h2>
              {summary.budgets.map((budget) => (
                <div key={budget.employee_id} className="border-b pb-4">
                  <BudgetStatus budget={budget} />
                </div>
              ))}
            </section>
          )}
          <dl className="grid gap-6 border-y py-6 sm:grid-cols-4">
            <div>
              <dt className="text-sm text-muted-foreground">Known spend</dt>
              <dd className="mt-2 text-2xl font-semibold">
                {usd(summary.total.known_spend_usd)}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">Calls</dt>
              <dd className="mt-2 text-2xl font-semibold">
                {summary.total.calls}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">
                Reported input tokens
              </dt>
              <dd className="mt-2 text-2xl font-semibold">
                {summary.total.input_tokens?.toLocaleString() ?? "Unavailable"}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">
                Reported output tokens
              </dt>
              <dd className="mt-2 text-2xl font-semibold">
                {summary.total.output_tokens?.toLocaleString() ?? "Unavailable"}
              </dd>
            </div>
          </dl>
          <div className="grid gap-8 xl:grid-cols-2">
            {breakdown(
              "By employee",
              summary.employees,
              summary.options.employees,
              "employee_id",
            )}
            {breakdown(
              "By agent",
              summary.agents,
              summary.options.agents,
              "agent_id",
            )}
          </div>
          <section aria-label="Provider calls">
            <h2 className="mb-3 font-semibold">Provider calls</h2>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Admitted (UTC)</TableHead>
                  <TableHead>Agent / employee</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Cost</TableHead>
                  <TableHead>Details</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {calls?.items.map((call) => (
                  <TableRow key={call.id}>
                    <TableCell className="whitespace-nowrap">
                      {new Date(call.admitted_at).toLocaleString("en-GB", {
                        timeZone: "UTC",
                      })}
                    </TableCell>
                    <TableCell>
                      {label(summary.options.agents, call.agent_id)}
                      <span className="block text-xs text-muted-foreground">
                        {label(summary.options.employees, call.employee_id)}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-56 break-words whitespace-normal">
                      {call.model}
                    </TableCell>
                    <TableCell>
                      {call.cost_usd === null
                        ? "Unavailable"
                        : usd(call.cost_usd)}
                    </TableCell>
                    <TableCell>
                      <details>
                        <summary className="cursor-pointer">
                          {call.outcome.replaceAll("_", " ")}
                        </summary>
                        <p className="mt-2 text-xs">
                          Input: {call.input_tokens ?? "unavailable"} · Output:{" "}
                          {call.output_tokens ?? "unavailable"} · Reasoning:{" "}
                          {call.reasoning_tokens ?? "unavailable"}
                        </p>
                        <p className="text-xs">
                          Duration:{" "}
                          {call.duration_ms === null
                            ? "unavailable"
                            : `${call.duration_ms} ms`}
                        </p>
                        {call.generation_id && (
                          <p className="max-w-48 break-all text-xs">
                            {call.generation_id}
                          </p>
                        )}
                      </details>
                    </TableCell>
                  </TableRow>
                ))}
                {calls?.items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5}>
                      No tracked calls match these filters.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
            <div className="mt-4 flex justify-end gap-3">
              <Button
                variant="outline"
                disabled={loading || cursors.length === 1}
                onClick={() => {
                  setLoading(true);
                  setCursors((v) => v.slice(0, -1));
                }}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                disabled={loading || !calls?.next_cursor}
                onClick={() => {
                  setLoading(true);
                  setCursors((v) => [...v, calls!.next_cursor!]);
                }}
              >
                Next
              </Button>
            </div>
          </section>
          <p className="text-xs text-muted-foreground">
            UTC calendar month ending{" "}
            {new Date(summary.period.end).toISOString().slice(0, 10)}. Refreshes
            every 15 seconds. Unknown costs are never counted as reported zero.
          </p>
        </>
      )}
    </section>
  );
}
