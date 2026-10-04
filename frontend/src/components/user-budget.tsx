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
        {budget.user_name}: {formatUsd(budget.known_spend_usd)} known spend
        ·{" "}
        {budget.monthly_budget_usd === null
          ? "Unlimited budget"
          : `${formatUsd(budget.monthly_budget_usd)} monthly budget`}
      </p>
      {budget.status === "warning" && (
        <Alert role="status">
          <AlertDescription>
            Approaching monthly budget (80% or more used). All agents
            assigned to this user share this budget.
          </AlertDescription>
        </Alert>
      )}
      {budget.status === "exhausted" && (
        <Alert role="alert">
          <AlertDescription>
            Monthly budget reached. New provider requests through Talos are
            blocked. Increase the budget in Users or wait for the next
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
        UTC. Already-admitted requests may exceed the budget.{" "}
        <a
          className="underline"
          href={`#usage?user_id=${budget.user_id}`}
        >
          View user usage
        </a>
      </p>
    </div>
  );
}

export function UserBudget({
  userId,
  editable = false,
}: {
  userId: string;
  editable?: boolean;
}) {
  const [budget, setBudget] = useState<Budget | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const limit =
    budget?.monthly_budget_usd
      ?.replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1")
      .replace(/^0E[+-]?\d+$/i, "0") ?? "";
  useEffect(() => {
    if (saving) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const data = await api<Budget>(`/users/${userId}/budget`, {
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
  }, [userId, saving]);
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const value = (draft ?? limit).trim();
      const data = await api<Budget>(`/users/${userId}/budget`, {
        method: "PUT",
        body: JSON.stringify({ monthly_budget_usd: value || null }),
      });
      setBudget(data);
      setDraft(null);
      setNotice("Budget saved. Applies to subsequent provider requests.");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="space-y-3" aria-label="Monthly budget">
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
        <p className="text-xs text-muted-foreground">Loading budget…</p>
      )}
      {editable && (
        <form onSubmit={save} className="space-y-3">
          <Label htmlFor={`limit-${userId}`}>
            Monthly budget (USD)
          </Label>
          <Input
            id={`limit-${userId}`}
            type="number"
            min="0"
            step="any"
            placeholder="Unlimited"
            value={draft ?? limit}
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
            {saving ? "Saving…" : "Save budget"}
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
  userId,
  coverage,
  agentId,
}: {
  userId: string | null;
  coverage: "gateway" | "external" | "simulator";
  agentId: string;
}) {
  if (coverage === "external")
    return (
      <p className="text-xs text-muted-foreground">
        Provider handled by agent: usage unavailable. Direct-provider traffic is
        outside Talos budget enforcement.
      </p>
    );
  if (coverage === "simulator")
    return (
      <p className="text-xs text-muted-foreground">
        The local simulator does not spend a user budget.
      </p>
    );
  if (!userId)
    return (
      <Alert role="alert">
        <AlertDescription>
          Assign a user to this agent.{" "}
          <a className="underline" href={`#agents/${agentId}/settings`}>
            Assign this agent in settings
          </a>{" "}
          before using a provider through Talos.
        </AlertDescription>
      </Alert>
    );
  return <UserBudget key={userId} userId={userId} />;
}
