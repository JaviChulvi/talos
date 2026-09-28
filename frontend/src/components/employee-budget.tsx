import { useEffect, useState, type FormEvent } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage, type Budget } from "@/lib/api";
import { formatUsd } from "@/lib/utils";

export function BudgetStatus({ budget }: { budget: Budget }) {
  return (
    <div className="space-y-3 text-sm">
      <p>
        {budget.employee_name}: {formatUsd(budget.known_spend_usd)} known spend
        ·{" "}
        {budget.monthly_allowance_usd === null
          ? "Unlimited allowance"
          : `${formatUsd(budget.monthly_allowance_usd)} monthly allowance`}
      </p>
      {budget.status === "warning" && (
        <Alert role="status">
          <AlertDescription>
            Approaching monthly allowance (80% or more used). All agents
            assigned to this employee share this allowance.
          </AlertDescription>
        </Alert>
      )}
      {budget.status === "exhausted" && (
        <Alert role="alert">
          <AlertDescription>
            Monthly allowance reached. New provider requests through Talos are
            blocked. Increase the allowance in Employees or wait for the next
            UTC month.
          </AlertDescription>
        </Alert>
      )}
      {(budget.unresolved_calls > 0 || budget.missing_cost_calls > 0) && (
        <p className="text-xs text-muted-foreground">
          Accounting is incomplete: {budget.missing_cost_calls} calls have no
          reported cost and {budget.unresolved_calls} are unresolved. Admission
          uses known spend; unknown charges are excluded.
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        Resets {new Date(budget.period.end).toISOString().slice(0, 10)} at 00:00
        UTC. Already-admitted requests may exceed the allowance.{" "}
        <a
          className="underline"
          href={`#usage?employee_id=${budget.employee_id}`}
        >
          View employee usage
        </a>
      </p>
    </div>
  );
}

export function EmployeeBudget({
  employeeId,
  editable = false,
}: {
  employeeId: string;
  editable?: boolean;
}) {
  const [budget, setBudget] = useState<Budget | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const allowance =
    budget?.monthly_allowance_usd
      ?.replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1")
      .replace(/^0E[+-]?\d+$/i, "0") ?? "";
  useEffect(() => {
    if (saving) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const data = await api<Budget>(`/employees/${employeeId}/budget`, {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(8000),
          ]),
        });
        if (!controller.signal.aborted) {
          setBudget(data);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      } finally {
        if (!controller.signal.aborted)
          timer = window.setTimeout(() => void poll(), 5000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [employeeId, saving]);
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const value = (draft ?? allowance).trim();
      const data = await api<Budget>(`/employees/${employeeId}/budget`, {
        method: "PUT",
        body: JSON.stringify({ monthly_allowance_usd: value || null }),
      });
      setBudget(data);
      setDraft(null);
      setNotice("Allowance saved. Applies to subsequent provider requests.");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="space-y-3" aria-label="Employee allowance">
      {error && (
        <Alert role="alert">
          <AlertDescription>
            {error} Budget status may be out of date.
          </AlertDescription>
        </Alert>
      )}
      {budget ? (
        <BudgetStatus budget={budget} />
      ) : (
        <p className="text-xs text-muted-foreground">Loading allowance…</p>
      )}
      {editable && (
        <form onSubmit={save} className="space-y-3">
          <Label htmlFor={`allowance-${employeeId}`}>
            Monthly allowance (USD)
          </Label>
          <Input
            id={`allowance-${employeeId}`}
            type="number"
            min="0"
            step="any"
            placeholder="Unlimited"
            value={draft ?? allowance}
            onChange={(event) => {
              setDraft(event.target.value);
              setNotice("");
            }}
            disabled={saving || !budget}
          />
          <p className="text-xs text-muted-foreground">
            Leave empty for unlimited. Zero blocks provider requests through
            Talos. Changes do not interrupt admitted calls.
          </p>
          <Button type="submit" disabled={saving || !budget}>
            {saving ? "Saving…" : "Save allowance"}
          </Button>
          {notice && (
            <p className="text-sm" role="status">
              {notice}
            </p>
          )}
        </form>
      )}
    </section>
  );
}

export function AgentBudget({
  employeeId,
  coverage,
  agentId,
}: {
  employeeId: string | null;
  coverage: "gateway" | "external" | "simulator";
  agentId: string;
}) {
  if (coverage === "external")
    return (
      <p className="text-xs text-muted-foreground">
        Provider handled by agent: usage unavailable. Direct-provider traffic is
        outside Talos allowance enforcement.
      </p>
    );
  if (coverage === "simulator")
    return (
      <p className="text-xs text-muted-foreground">
        The local simulator does not spend an employee allowance.
      </p>
    );
  if (!employeeId)
    return (
      <Alert role="alert">
        <AlertDescription>
          Employee assignment required.{" "}
          <a className="underline" href={`#agents/${agentId}/settings`}>
            Assign this agent in settings
          </a>{" "}
          before using a provider through Talos.
        </AlertDescription>
      </Alert>
    );
  return <EmployeeBudget key={employeeId} employeeId={employeeId} />;
}
