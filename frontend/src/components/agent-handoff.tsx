import { useEffect, useState, type FormEvent } from "react";
import {
  Check,
  CircleAlert,
  Copy,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  api,
  errorMessage,
  type AgentAvailability,
  type AvailabilityEvidence,
  type UserAccess,
  type UserChannel,
} from "@/lib/api";

type Agent = {
  id: string;
  user_id: string | null;
  user_name: string;
  display_name: string;
  runtime_mode: string;
  desired_state: string;
  observed_state: string;
};
type CheckResult = {
  id: string;
  status: string;
  output?: string;
  code?: string;
  error?: string | null;
};
type Invitation = { token: string; expires_at: string };
type HandoffEvidence = {
  id: string;
  current: boolean;
  received_at: string | null;
  transport_state: string;
  transport_accepted_at: string | null;
  verified_at: string | null;
  receipt: {
    run_id: string;
    accepted_parts: number;
    total_parts: number;
  } | null;
};

const states: Record<string, string> = {
  ok: "Ready",
  available: "Available",
  requires_action: "Requires action",
  unknown: "Unverified",
  unverified: "Unverified",
  stale: "Check expired",
  checking: "Checking",
  blocked: "Requires action",
  stopped: "Stopped",
  not_applicable: "Not required",
  pending: "Pending approval",
  active: "Approved",
  disabled: "Disabled",
};
const causes: Record<string, string> = {
  native_safe_probe_unavailable:
    "This native provider has no safe tools-free test. Verify a normal conversation, or select a model through Talos.",
  invalid_credentials:
    "The provider rejected these credentials. Save a new token and check again.",
  missing_scope:
    "Reinstall the Slack app with the permissions in the Talos manifest.",
  app_token_mismatch:
    "The bot and app tokens belong to different Slack apps. Save the matching pair.",
  workspace_mismatch: "Use tokens installed in the configured Slack workspace.",
  consumer_conflict:
    "Another consumer is connected. Disable the other polling or Socket Mode consumer.",
  webhook_conflict:
    "This Telegram bot has a webhook. Remove it in its owning integration before using Talos polling.",
  connector_unavailable:
    "Check the connector service, then check this channel again.",
  budget_exhausted: "Review the user's monthly budget.",
  budget_or_credit_exhausted:
    "Review the user budget and provider balance.",
  model_unavailable: "Choose an available model in agent settings.",
  model_send_uncertain:
    "The test timed out after sending. Stop the agent before trying again.",
  connections_tools_changed:
    "Expected MCP tools are missing. Review the endpoint and credential, then check again.",
  connections_connection:
    "A selected setup credential is missing. Correct the connection and apply the setup.",
};

function State({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "ok" || value === "available" || value === "active"
          ? "success"
          : "secondary"
      }
    >
      {value === "ok" || value === "available" ? (
        <Check className="size-3" />
      ) : null}
      {states[value] ?? value.replaceAll("_", " ")}
    </Badge>
  );
}

function evidenceState(check: AvailabilityEvidence, now: number) {
  return check.state !== "not_applicable" &&
    check.expires_at &&
    new Date(check.expires_at).getTime() <= now
    ? "stale"
    : check.state;
}

function Evidence({
  check,
  now,
}: {
  check: AvailabilityEvidence;
  now: number;
}) {
  const state = evidenceState(check, now);
  return (
    <div className="space-y-1 text-sm">
      <State value={state} />
      {state !== "ok" && state !== "not_applicable" && (
        <p className="text-muted-foreground">
          {causes[check.code ?? ""] ?? check.action}
        </p>
      )}
      {check.checked_at && (
        <p className="text-xs text-muted-foreground">
          Checked {new Date(check.checked_at).toLocaleTimeString()}
          {check.expires_at
            ? ` · valid until ${new Date(check.expires_at).toLocaleTimeString()}`
            : ""}
        </p>
      )}
    </div>
  );
}

export function AgentHandoff({
  agent,
  disabled,
}: {
  agent: Agent;
  disabled: boolean;
}) {
  const [availability, setAvailability] = useState<AgentAvailability | null>(
    null,
  );
  const [channels, setChannels] = useState<UserChannel[]>([]);
  const [accesses, setAccesses] = useState<UserAccess[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [notice, setNotice] = useState("");
  const [probe, setProbe] = useState<CheckResult | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => {
      Promise.all([
        api<AgentAvailability>(`/agents/${agent.id}/availability`, {
          signal: controller.signal,
        }),
        api<UserChannel[]>("/channels", { signal: controller.signal }),
        api<UserAccess[]>("/user-accesses", {
          signal: controller.signal,
        }),
      ])
        .then(([ready, configured, bindings]) => {
          setAvailability(ready);
          setChannels(configured);
          setAccesses(bindings);
          setLoadError(null);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setLoadError(errorMessage(cause));
        });
    };
    load();
    const timer = window.setInterval(() => {
      setNow(Date.now());
      load();
    }, 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [agent.id, refresh]);

  useEffect(() => {
    if (
      !probe ||
      !["queued", "dispatching", "running", "cancel_requested"].includes(
        probe.status,
      )
    )
      return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      api<CheckResult>(`/runs/${probe.id}`, { signal: controller.signal })
        .then((result) => {
          setProbe(result);
          setRefresh((value) => value + 1);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(errorMessage(cause));
        });
    }, 1500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [probe]);

  async function check(kind: "runtime" | "connections" | "model") {
    setBusy(true);
    setError(null);
    try {
      const result = await api<CheckResult>(`/agents/${agent.id}/checks`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ kind }),
      });
      setProbe(result);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  const checking =
    !!probe &&
    ["queued", "dispatching", "running", "cancel_requested"].includes(
      probe.status,
    );
  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Access & availability</h2>
          <p className="mt-1 max-w-prose text-sm text-muted-foreground">
            Prepare {agent.user_name || "the user"}'s access to{" "}
            {agent.display_name}. Users talk to the agent in Telegram or
            Slack; Talos stays with the administrator.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw />
          Refresh
        </Button>
      </div>
      {(error || loadError) && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertDescription>{error || loadError}</AlertDescription>
        </Alert>
      )}
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      <section
        aria-labelledby="readiness-heading"
        className="rounded-lg border p-5"
      >
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h3 id="readiness-heading" className="font-medium">
            Current availability
          </h3>
          {availability && (
            <State
              value={
                loadError ||
                (availability.status === "available" &&
                  availability.checks.some(
                    (item) =>
                      !["ok", "not_applicable"].includes(
                        evidenceState(item, now),
                      ),
                  ))
                  ? "unverified"
                  : availability.status
              }
            />
          )}
        </div>
        {!availability ? (
          <Skeleton className="h-24" />
        ) : (
          <ul className="divide-y">
            {availability.checks.map((item) => (
              <li
                key={item.kind}
                className="flex flex-wrap items-start justify-between gap-3 py-3"
              >
                <span className="capitalize text-sm font-medium">
                  {item.kind}
                </span>
                <Evidence check={item} now={now} />
              </li>
            ))}
          </ul>
        )}
        {availability?.update_available && (
          <p className="mt-3 text-sm text-muted-foreground">
            A setup update is pending. Review and apply it from Permissions.
          </p>
        )}
        <div className="mt-4 flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={
              disabled || busy || checking || agent.observed_state !== "ready"
            }
            onClick={() => void check("runtime")}
          >
            Check runtime
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={
              disabled || busy || checking || agent.observed_state !== "ready"
            }
            onClick={() => void check("connections")}
          >
            Check connections
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={
              disabled || busy || checking || agent.observed_state !== "ready"
            }
            onClick={() => void check("model")}
          >
            {checking ? <LoaderCircle className="animate-spin" /> : null}Test
            model · uses credit
          </Button>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          A model test can consume provider credit. Checks wait for no active
          conversation; they never interrupt a running turn.
        </p>
        {probe?.status === "unknown" && (
          <p role="status" className="mt-3 text-sm">
            The test is uncertain. Stop the agent before retrying.
          </p>
        )}
      </section>
      {agent.runtime_mode !== "native" || !agent.user_id ? (
        <Alert>
          <CircleAlert />
          <AlertDescription>
            Assign a user to a native agent in Permissions before preparing
            channel access.
          </AlertDescription>
        </Alert>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          {(["telegram", "slack"] as const).map((provider) => (
            <ChannelHandoff
              key={provider}
              provider={provider}
              agent={agent}
              channel={channels.find((item) => item.provider === provider)}
              access={accesses.find(
                (item) =>
                  item.user_id === agent.user_id &&
                  item.channel_id ===
                    channels.find((channel) => channel.provider === provider)
                      ?.id,
              )}
              evidence={
                availability?.accesses.find(
                  (item) => item.provider === provider,
                )?.channel ??
                channels.find((item) => item.provider === provider)
                  ?.availability
              }
              disabled={disabled || busy || !!loadError}
              now={now}
              onChange={() => setRefresh((value) => value + 1)}
              onNotice={setNotice}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ChannelHandoff({
  provider,
  agent,
  channel,
  access,
  evidence,
  disabled,
  now,
  onChange,
  onNotice,
}: {
  provider: "telegram" | "slack";
  agent: Agent;
  channel?: UserChannel;
  access?: UserAccess;
  evidence?: AvailabilityEvidence;
  disabled: boolean;
  now: number;
  onChange: () => void;
  onNotice: (message: string) => void;
}) {
  const [tokens, setTokens] = useState<Record<string, string>>({});
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [invitation, setInvitation] = useState<Invitation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<CheckResult | null>(null);
  const [editing, setEditing] = useState(false);
  const title = provider === "telegram" ? "Telegram" : "Slack";

  useEffect(() => {
    if (!probe || !["queued", "running"].includes(probe.status)) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      api<CheckResult>(`/channel-checks/${probe.id}`, {
        signal: controller.signal,
      })
        .then((result) => {
          setProbe(result);
          onChange();
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(errorMessage(cause));
        });
    }, 1500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [probe, onChange]);

  async function action(task: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await task();
      onChange();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function credentials(event: FormEvent) {
    event.preventDefault();
    await action(async () => {
      const configured =
        channel ??
        (await api<UserChannel>("/channels", {
          method: "POST",
          body: JSON.stringify({
            provider,
            name: title,
            workspace_id: provider === "slack" ? (workspace ?? "") : "",
          }),
        }));
      onChange(); // Preserve a created channel if saving its credentials fails.
      await api(`/channels/${configured.id}/credentials`, {
        method: "PUT",
        body: JSON.stringify({ values: tokens }),
      });
      setTokens({});
      setEditing(false);
      onNotice(
        `${title} credentials saved. Check the channel before enabling access.`,
      );
    });
  }

  async function saveIdentity(event: FormEvent) {
    event.preventDefault();
    await action(async () => {
      if (!channel) return;
      await api(
        access ? `/user-accesses/${access.id}` : "/user-accesses",
        {
          method: access ? "PUT" : "POST",
          body: JSON.stringify({
            channel_id: channel.id,
            user_id: agent.user_id,
            agent_id: agent.id,
            external_scope: channel.workspace_id,
            external_user_id: userId.trim() || access?.external_user_id || null,
          }),
        },
      );
      setInvitation(null);
      onNotice(
        "Identity saved as pending. Review the external ID before approving it.",
      );
    });
  }

  const link =
    provider === "telegram" && channel?.identity.username
      ? `https://t.me/${channel.identity.username}${invitation ? `?start=${encodeURIComponent(invitation.token)}` : ""}`
      : provider === "slack" && channel?.identity.app_id
        ? `https://slack.com/app_redirect?app=${encodeURIComponent(channel.identity.app_id)}&team=${encodeURIComponent(channel.workspace_id)}`
        : null;
  const invitationValid =
    invitation && new Date(invitation.expires_at).getTime() > now;
  const instructions = link
    ? `Open ${link}\n${provider === "slack" && invitationValid ? `Send register ${invitation.token}.\n` : ""}${access?.state === "active" ? `Send a private text message to ${agent.display_name}.` : "Register your identity, then wait for your administrator to approve access."}`
    : "";
  const checking = !!probe && ["queued", "running"].includes(probe.status);
  return (
    <section
      aria-label={`${title} user access`}
      className="space-y-5 rounded-lg border p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{title}</h3>
        <State
          value={
            channel?.enabled
              ? evidence
                ? evidenceState(evidence, now)
                : "unknown"
              : "disabled"
          }
        />
      </div>
      <p className="text-sm text-muted-foreground">
        {provider === "telegram"
          ? "One shared bot. Private text messages only."
          : "One internal app in the configured workspace. Private DMs only."}
      </p>
      {error && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {!channel?.credentials_configured || editing ? (
        <form
          onSubmit={(event) => void credentials(event)}
          className="space-y-3"
        >
          {provider === "slack" && !channel && (
            <div className="space-y-1">
              <Label htmlFor="slack-workspace">Workspace ID</Label>
              <Input
                id="slack-workspace"
                value={workspace ?? ""}
                onChange={(event) => setWorkspace(event.target.value)}
                required
                pattern="T[A-Z0-9]+"
                placeholder="T0123456789"
                disabled={disabled || busy}
              />
            </div>
          )}
          {(provider === "telegram"
            ? ["bot_token"]
            : ["bot_token", "app_token"]
          ).map((field) => (
            <div key={field} className="space-y-1">
              <Label htmlFor={`${provider}-${field}`}>
                {field === "app_token"
                  ? "App token · connections:write"
                  : "Bot token"}
              </Label>
              <Input
                id={`${provider}-${field}`}
                type="password"
                autoComplete="new-password"
                value={tokens[field] ?? ""}
                onChange={(event) =>
                  setTokens({ ...tokens, [field]: event.target.value })
                }
                required
                disabled={disabled || busy}
              />
            </div>
          ))}
          <p className="text-xs text-muted-foreground">
            Channel credentials are shared across this installation. Rotating them
            affects every user using this channel.
          </p>
          {provider === "slack" && (
            <p className="text-xs text-muted-foreground">
              Install the internal app with im:history, chat:write and
              users:read. The bot and app tokens must belong to the same app.
            </p>
          )}
          <Button size="sm" disabled={disabled || busy}>
            Save credentials
          </Button>
          {editing && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setTokens({});
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          )}
        </form>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || busy || checking}
            onClick={() =>
              void action(async () => {
                setProbe(
                  await api<CheckResult>(`/channels/${channel.id}/check`, {
                    method: "POST",
                    headers: { "Idempotency-Key": crypto.randomUUID() },
                  }),
                );
              })
            }
          >
            {checking ? <LoaderCircle className="animate-spin" /> : null}Check
            channel
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || busy}
            onClick={() =>
              void action(async () => {
                await api(`/channels/${channel.id}`, {
                  method: "PUT",
                  body: JSON.stringify({
                    name: channel.name,
                    enabled: !channel.enabled,
                  }),
                });
              })
            }
          >
            {channel.enabled ? "Disable channel" : "Enable channel"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={disabled || busy}
            onClick={() => setEditing(true)}
          >
            Rotate credentials
          </Button>
        </div>
      )}
      {evidence && <Evidence check={evidence} now={now} />}
      {probe && !checking && (
        <p role="status" className="text-sm">
          {probe.status === "completed"
            ? "Channel check completed."
            : (causes[probe.code ?? ""] ??
              "Check could not complete. Review credentials and channel configuration.")}
        </p>
      )}
      {channel?.provider === "slack" && (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void action(async () => {
              await api(`/channels/${channel.id}`, {
                method: "PUT",
                body: JSON.stringify({
                  name: channel.name,
                  enabled: false,
                  workspace_id: workspace ?? channel.workspace_id,
                }),
              });
              setInvitation(null);
              setProbe(null);
              onNotice(
                "Workspace saved. Check and enable the channel, then save and approve user identities again.",
              );
            });
          }}
        >
          <Label htmlFor="slack-workspace-edit">Workspace ID</Label>
          <Input
            id="slack-workspace-edit"
            value={workspace ?? channel.workspace_id}
            onChange={(event) => setWorkspace(event.target.value)}
            required
            pattern="T[A-Z0-9]{2,39}"
            disabled={disabled || busy}
          />
          <p className="text-xs text-muted-foreground">
            Changing the workspace disables Slack for every user until the
            channel is checked and their identities are approved again.
          </p>
          <Button
            size="sm"
            variant="outline"
            disabled={
              disabled ||
              busy ||
              (workspace ?? channel.workspace_id).trim() === channel.workspace_id
            }
          >
            Save workspace
          </Button>
        </form>
      )}
      {channel && (
        <div className="space-y-3 border-t pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-medium">User identity</h4>
            {access && <State value={access.state} />}
          </div>
          {access?.external_user_id && (
            <p className="break-all text-sm">
              External ID:{" "}
              <span className="font-mono">{access.external_user_id}</span>
            </p>
          )}
          {access && access.agent_id !== agent.id && (
            <Alert>
              <CircleAlert />
              <AlertDescription>
                This user's {title} access currently points to another
                agent. Saving below moves the destination here and requires
                approval again.
              </AlertDescription>
            </Alert>
          )}
          <form
            onSubmit={(event) => void saveIdentity(event)}
            className="space-y-2"
          >
            <Label htmlFor={`${provider}-identity`}>
              {provider === "telegram" ? "Telegram user ID" : "Slack member ID"}{" "}
              · optional
            </Label>
            <Input
              id={`${provider}-identity`}
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
              pattern={
                provider === "telegram" ? "[1-9][0-9]*" : "[UW][A-Z0-9]+"
              }
              placeholder={
                provider === "telegram" ? "123456789" : "U0123456789"
              }
              disabled={disabled || busy}
            />
            <p className="text-xs text-muted-foreground">
              Use a stable platform ID. Leave blank to keep an existing ID or
              invite a new user. Saving resets approval.
            </p>
            <Button variant="outline" size="sm" disabled={disabled || busy}>
              {access ? "Save identity / destination" : "Create pending access"}
            </Button>
          </form>
          {access && access.agent_id === agent.id && (
            <div className="flex flex-wrap gap-2">
              {access.state !== "active" && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={disabled || busy || !link}
                  onClick={() =>
                    void action(async () => {
                      setInvitation(
                        await api<Invitation>(
                          `/user-accesses/${access.id}/invitation`,
                          { method: "POST" },
                        ),
                      );
                    })
                  }
                >
                  Create invitation
                </Button>
              )}
              {access.state !== "active" ? (
                <Button
                  size="sm"
                  disabled={disabled || busy || !access.external_user_id}
                  onClick={() =>
                    void action(async () => {
                      await api(`/user-accesses/${access.id}/approve`, {
                        method: "POST",
                      });
                      setInvitation(null);
                    })
                  }
                >
                  Approve identity
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={disabled || busy}
                  onClick={() =>
                    void action(async () => {
                      await api(`/user-accesses/${access.id}/disable`, {
                        method: "POST",
                      });
                      setInvitation(null);
                    })
                  }
                >
                  Revoke access
                </Button>
              )}
            </div>
          )}
        </div>
      )}
      {invitation && (
        <p role="status" className="text-xs text-muted-foreground">
          {invitationValid
            ? `Invitation expires at ${new Date(invitation.expires_at).toLocaleTimeString()}. It requests registration; approval remains with you.`
            : "Invitation expired. Create a new one."}
        </p>
      )}
      {access && access.agent_id === agent.id && (
        <HandoffVerification
          key={`${access.id}:${access.revision}:${channel?.revision}`}
          access={access}
          provider={provider}
          enabled={!!channel?.enabled && !!channel?.verified}
          disabled={disabled || busy}
          now={now}
        />
      )}
      {link &&
        access?.agent_id === agent.id &&
        (access.state === "active" || invitationValid) && (
          <div className="space-y-3 border-t pt-4">
            <h4 className="text-sm font-medium">Share with the user</h4>
            {!channel?.enabled && (
              <p className="text-sm text-muted-foreground">
                Enable this channel before the user uses these instructions.
              </p>
            )}
            <p className="whitespace-pre-line break-all text-sm text-muted-foreground">
              {instructions}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  navigator.clipboard
                    .writeText(instructions)
                    .then(() => onNotice(`${title} instructions copied.`))
                    .catch(() =>
                      setError(
                        "Copy was blocked. Select the instructions above and copy them manually.",
                      ),
                    );
                }}
              >
                <Copy />
                Copy instructions
              </Button>
              <Button variant="ghost" size="sm" asChild>
                <a href={link} target="_blank" rel="noreferrer">
                  <ExternalLink />
                  Open {title}
                </a>
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Share these instructions yourself. A verified credential does not
              prove that the user received an agent response.
            </p>
          </div>
        )}
    </section>
  );
}

function HandoffVerification({
  access,
  provider,
  enabled,
  disabled,
  now,
}: {
  access: UserAccess;
  provider: "telegram" | "slack";
  enabled: boolean;
  disabled: boolean;
  now: number;
}) {
  const [history, setHistory] = useState<HandoffEvidence[]>([]);
  const [challenge, setChallenge] = useState<Invitation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => {
      api<{ history: HandoffEvidence[] }>(
        `/user-accesses/${access.id}/handoff`,
        { signal: controller.signal },
      )
        .then((result) => {
          setHistory(result.history);
          setError(null);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(errorMessage(cause));
        });
    };
    load();
    const timer = window.setInterval(load, 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [access.id]);
  const latest = history[0];
  const instructions =
    challenge &&
    new Date(challenge.expires_at).getTime() > now &&
    enabled &&
    access.state === "active"
      ? `In your private chat, send ${provider === "telegram" ? "/verify" : "verify"} ${challenge.token}\nAfter the channel confirmation, send a normal text message to your agent.`
      : null;
  return (
    <div className="space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium">Verify delivery</h4>
        <Badge
          variant={
            latest?.verified_at && latest.current && !error
              ? "success"
              : "secondary"
          }
        >
          {error
            ? "Unverified"
            : latest?.verified_at
              ? latest.current
                ? "Delivery verified"
                : "Configuration changed"
              : "Not yet verified"}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        The channel test uses no model. The user's following message uses
        the agent normally and can consume credit. Provider acceptance does not
        prove the message was read.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <ol className="list-decimal space-y-1 pl-5 text-sm">
        <li>
          Identity:{" "}
          {access.state === "active" ? "approved" : "requires approval"}.
        </li>
        <li>
          Transport:{" "}
          {error
            ? "unverified"
            : latest?.transport_accepted_at
              ? "confirmation accepted by provider"
              : latest?.received_at
                ? `received · ${latest.transport_state}`
                : "waiting for user test"}
          .
        </li>
        <li>
          Agent reply:{" "}
          {error
            ? "unverified"
            : latest?.receipt
              ? `${latest.receipt.accepted_parts}/${latest.receipt.total_parts} parts accepted by provider`
              : "waiting for a completed reply after the test"}
          .
        </li>
        <li>
          Current availability: see the checks above. A delivery receipt remains
          historical.
        </li>
      </ol>
      {latest?.verified_at && (
        <p className="text-xs text-muted-foreground">
          Verified {new Date(latest.verified_at).toLocaleString()}.{" "}
          {latest.current
            ? "Recorded for this Talos configuration."
            : "Repeat the test for the current identity, channel or agent configuration."}
        </p>
      )}
      <Button
        variant="outline"
        size="sm"
        disabled={disabled || busy || !enabled || access.state !== "active"}
        onClick={() => {
          setBusy(true);
          setError(null);
          api<Invitation>(`/user-accesses/${access.id}/challenge`, {
            method: "POST",
          })
            .then((result) => setChallenge(result))
            .catch((cause) => setError(errorMessage(cause)))
            .finally(() => setBusy(false));
        }}
      >
        Create transport test
      </Button>
      {instructions && (
        <div className="space-y-2">
          <p className="whitespace-pre-line break-all text-sm">
            {instructions}
          </p>
          <p className="text-xs text-muted-foreground">
            Single use. Expires at{" "}
            {new Date(challenge!.expires_at).toLocaleTimeString()}. The
            following reply completes the verification.
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              navigator.clipboard
                .writeText(instructions)
                .catch(() =>
                  setError(
                    "Copy was blocked. Select the instructions and copy manually.",
                  ),
                );
            }}
          >
            <Copy />
            Copy test instructions
          </Button>
        </div>
      )}
      {challenge && !instructions && (
        <p className="text-xs text-muted-foreground">
          Test expired or access changed. Create a new test after checking
          access.
        </p>
      )}
      {history.length > 1 && (
        <details className="text-xs text-muted-foreground">
          <summary>Previous tests</summary>
          <ul className="mt-2 space-y-1">
            {history.slice(1).map((item) => (
              <li key={item.id}>
                {item.verified_at
                  ? `Reply accepted ${new Date(item.verified_at).toLocaleString()}`
                  : `Transport ${item.transport_state}`}{" "}
                ·{" "}
                {item.current ? "same configuration" : "configuration changed"}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
