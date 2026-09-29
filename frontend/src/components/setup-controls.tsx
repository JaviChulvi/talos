import { useRef, useState } from "react";
import { Copy, CircleAlert } from "lucide-react";
import {
  api,
  errorMessage,
  type AgentPermissions,
  type SetupPreview,
} from "@/lib/api";
import type { Setup, SetupSlot } from "@/components/setups";
import type { Connection } from "@/components/connections";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function ConnectionBindings({
  slots,
  connections,
  value,
  onChange,
  disabled,
  overrides = false,
}: {
  slots: SetupSlot[];
  connections: Connection[];
  value: Record<string, string>;
  onChange: (value: Record<string, string>) => void;
  disabled?: boolean;
  overrides?: boolean;
}) {
  if (!slots.length) return null;
  return (
    <fieldset disabled={disabled} className="space-y-4">
      <legend className="mb-2 font-medium">
        {overrides ? "Employee connections" : "Default connections"}
      </legend>
      <p className="text-xs text-muted-foreground">
        {overrides
          ? "Use the role’s account, or choose a connection for this employee."
          : "Choose the shared account for each slot. Agents validate required fields before applying."}
      </p>
      {slots.map((slot) => (
        <div key={slot.id}>
          <Label
            htmlFor={`binding-${overrides ? "employee" : "role"}-${slot.id}`}
          >
            {slot.label || slot.id}
          </Label>
          <Select
            disabled={disabled}
            value={value[slot.id] || "inherit"}
            onValueChange={(id) => {
              const next = { ...value };
              if (id === "inherit") delete next[slot.id];
              else next[slot.id] = id;
              onChange(next);
            }}
          >
            <SelectTrigger
              className="mt-2 w-full"
              id={`binding-${overrides ? "employee" : "role"}-${slot.id}`}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="inherit">
                {overrides ? "Use role default" : "No connection"}
              </SelectItem>
              {connections.map((connection) => (
                <SelectItem key={connection.id} value={connection.id}>
                  {connection.name}
                  {slot.fields.every((field) =>
                    connection.fields.includes(field),
                  )
                    ? ""
                    : " · missing fields"}
                </SelectItem>
              ))}
              {value[slot.id] &&
                !connections.some(
                  (connection) => connection.id === value[slot.id],
                ) && (
                  <SelectItem value={value[slot.id]}>
                    Unavailable connection
                  </SelectItem>
                )}
            </SelectContent>
          </Select>
          <p className="mt-1 text-xs text-muted-foreground">
            Required fields: {slot.fields.join(", ")}
          </p>
        </div>
      ))}
      <a
        href="#settings"
        className="block text-xs underline underline-offset-4"
      >
        Manage connections in Settings
      </a>
    </fieldset>
  );
}

export function RoleSetupFields({
  setups,
  connections,
  revisionId,
  grants,
  bindings,
  disabled,
  onRevision,
  onGrants,
  onBindings,
}: {
  setups: Setup[];
  connections: Connection[];
  revisionId: string;
  grants: string[];
  bindings: Record<string, string>;
  disabled?: boolean;
  onRevision: (id: string) => void;
  onGrants: (value: string[]) => void;
  onBindings: (value: Record<string, string>) => void;
}) {
  const revision = setups
    .flatMap((setup) => setup.revisions)
    .find((item) => item.id === revisionId);
  return (
    <div className="space-y-5 border-y py-5">
      <div>
        <Label htmlFor="role-setup">Setup version</Label>
        <Select
          value={revisionId || "none"}
          disabled={disabled}
          onValueChange={(id) => {
            const next = id === "none" ? "" : id;
            onRevision(next);
            const selected = setups
              .flatMap((setup) => setup.revisions)
              .find((item) => item.id === next);
            onGrants(
              selected?.manifest.connectors
                .filter((connector) => connector.enabled !== false)
                .map((connector) => connector.id) ?? [],
            );
            onBindings({});
          }}
        >
          <SelectTrigger id="role-setup" className="mt-2 w-full">
            <SelectValue placeholder="Choose a published setup" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">No setup</SelectItem>
            {setups.flatMap((setup) =>
              setup.revisions.map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {setup.name} · v{item.version}
                </SelectItem>
              )),
            )}
            {revisionId && !revision && (
              <SelectItem value={revisionId}>
                Unavailable setup version
              </SelectItem>
            )}
          </SelectContent>
        </Select>
        <p className="mt-2 text-xs text-muted-foreground">
          Publish a setup, select its version here, then explicitly apply this
          role to update agents.{" "}
          <a className="underline" href="#setups">
            Manage setups
          </a>
        </p>
      </div>
      {revision && (
        <>
          {!!revision.manifest.skills.filter((skill) => skill.enabled !== false)
            .length && (
            <p className="text-sm text-muted-foreground">
              Included skills automatically grant OpenClaw native read access
              or Hermes skill access. Enable terminal execution separately
              for skills that run commands.
            </p>
          )}
          {!!revision.manifest.connectors.length && (
            <fieldset disabled={disabled}>
              <legend className="mb-2 font-medium">Allowed connectors</legend>
              <div className="space-y-3">
                {revision.manifest.connectors
                  .filter((connector) => connector.enabled !== false)
                  .map((connector) => (
                    <Label
                      key={connector.id}
                      className="flex items-start gap-3"
                    >
                      <Checkbox
                        className="mt-0.5"
                        checked={grants.includes(connector.id)}
                        disabled={disabled}
                        onCheckedChange={(checked) =>
                          onGrants(
                            checked === true
                              ? [...grants, connector.id]
                              : grants.filter((id) => id !== connector.id),
                          )
                        }
                      />
                      <span>
                        <span className="block">
                          {connector.name || connector.id}
                        </span>
                        <span className="mt-1 block text-xs text-muted-foreground">
                          {connector.tools.join(", ")}
                        </span>
                      </span>
                    </Label>
                  ))}
              </div>
            </fieldset>
          )}
          <ConnectionBindings
            slots={revision.manifest.connection_slots}
            connections={connections}
            value={bindings}
            onChange={onBindings}
            disabled={disabled}
          />
        </>
      )}
    </div>
  );
}

const statusLabels: Record<string, string> = {
  not_configured: "No setup selected",
  pending: "Pending application",
  installed: "Installed",
  needs_connection: "Needs connection",
  update_available: "Update available",
  verified_ready: "Verified ready",
  blocked: "Blocked",
};
export function SetupStatus({ agent }: { agent: AgentPermissions }) {
  const selected = agent.selected_application?.setup;
  const applied = agent.applied_application?.setup;
  const status = agent.setup_status ?? "not_configured";
  return (
    <section
      className="space-y-3 border-y py-5"
      aria-label="Agent setup status"
    >
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="font-semibold">Reproducible setup</h3>
        <Badge
          variant={
            status === "verified_ready"
              ? "success"
              : ["blocked", "needs_connection", "update_available"].includes(
                    status,
                  )
                ? "warning"
                : "secondary"
          }
        >
          {statusLabels[status] ?? status.replaceAll("_", " ")}
        </Badge>
      </div>
      <dl className="grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="font-medium">Selected version</dt>
          <dd className="mt-1 text-muted-foreground">
            {selected?.version
              ? `Version ${selected.version}`
              : "No setup selected"}
          </dd>
        </div>
        <div>
          <dt className="font-medium">Successfully applied</dt>
          <dd className="mt-1 text-muted-foreground">
            {applied?.version
              ? `Version ${applied.version}`
              : "Not applied yet"}
          </dd>
        </div>
      </dl>
      {!!agent.setup_blockers?.length && (
        <Alert>
          <CircleAlert />
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-4">
              {agent.setup_blockers.map((blocker, index) => (
                <li key={index}>{blocker}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <p className="text-xs text-muted-foreground">
        Starting uses the selected configuration. Apply the saved role to change
        its setup, permissions, or account versions.
      </p>
    </section>
  );
}

export function ApplyPreview({
  previews,
  loading,
}: {
  previews: { name: string; preview: SetupPreview }[];
  loading?: boolean;
}) {
  return (
    <div
      className="max-h-72 space-y-4 overflow-y-auto text-sm"
      aria-live="polite"
    >
      {loading ? (
        <p className="text-muted-foreground">
          Checking setup, permissions, and connections…
        </p>
      ) : (
        previews.map(({ name, preview }, index) => (
          <div key={`${name}-${index}`}>
            <h3 className="font-medium">{name}</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-muted-foreground">
              {preview.changes.length ? (
                preview.changes.map((change, index) => (
                  <li key={index}>{change}</li>
                ))
              ) : (
                <li>Reapply the saved role configuration.</li>
              )}
            </ul>
            {!!preview.blockers.length && (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-danger">
                {preview.blockers.map((blocker, index) => (
                  <li key={index}>{blocker}</li>
                ))}
              </ul>
            )}
          </div>
        ))
      )}
    </div>
  );
}

export function CaptureSetup({
  agent,
  disabled,
  operation,
  onOperation,
}: {
  agent: AgentPermissions;
  disabled: boolean;
  operation: {
    id: string;
    action?: string;
    status: string;
    error?: string | null;
    result?: { setup_id?: string } | null;
  } | null;
  onOperation: (agentId: string, id: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const retry = useRefKey();
  const captured = operation?.action === "capture_setup" ? operation : null;
  async function capture() {
    setSubmitting(true);
    setError(null);
    try {
      const result = await api<{ id: string }>(
        `/agents/${agent.id}/capture-setup`,
        { method: "POST", headers: { "Idempotency-Key": retry.key() } },
      );
      onOperation(agent.id, result.id);
      retry.clear();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <section className="border-t pt-5">
      <h3 className="font-semibold">Reuse this agent’s setup</h3>
      <p className="mt-2 max-w-prose text-sm text-muted-foreground">
        Capture its supported instructions, skills, and tools into a draft.
        Review the draft before publishing. Account credentials and
        conversations stay here.
      </p>
      <Button
        className="mt-4"
        variant="outline"
        disabled={
          disabled ||
          submitting ||
          agent.observed_state !== "stopped" ||
          agent.desired_state !== "stopped"
        }
        onClick={() => void capture()}
      >
        <Copy />
        {submitting ? "Requesting capture…" : "Create setup from this agent"}
      </Button>
      {agent.observed_state !== "stopped" && (
        <p className="mt-2 text-xs text-muted-foreground">
          Stop this agent and wait for active work to finish before capturing.
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {error} Retry to resume the same request.
        </p>
      )}
      {captured && (
        <p role="status" className="mt-3 text-sm">
          {captured.result?.setup_id ? (
            <a
              href={`#setups/${captured.result.setup_id}`}
              className="underline underline-offset-4"
            >
              Review captured draft
            </a>
          ) : (
            captured.error || `Capture ${captured.status.replaceAll("_", " ")}`
          )}
        </p>
      )}
    </section>
  );
}

// A retry reuses the admission key until the API confirms its operation.
function useRefKey() {
  const holder = useRef<string | null>(null);
  return {
    key: () => holder.current ?? (holder.current = crypto.randomUUID()),
    clear: () => {
      holder.current = null;
    },
  };
}
