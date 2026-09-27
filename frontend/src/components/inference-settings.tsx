import { useEffect, useId, useState, type FormEvent } from "react";
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
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  GenerationControls,
  type GenerationSettings,
  type InferenceSelection,
  type ModelCapabilities,
} from "@/components/generation-settings";
import { ModelPicker } from "@/components/model-picker";
import { api, ApiError, errorMessage } from "@/lib/api";

type Selection = InferenceSelection & { inherited?: boolean };
type Draft = { model_id: string; settings: GenerationSettings };

type ProviderStatus = { configured: boolean; source: "app" | "deployment" };

export function OpenRouterSettings() {
  const id = useId();
  const [status, setStatus] = useState<ProviderStatus | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<ProviderStatus>("/inference/provider", {
      signal: controller.signal,
    })
      .then((value) => {
        setStatus(value);
        setError(null);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [reload]);
  async function save(remove = false) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const value = await api<ProviderStatus>("/inference/provider", {
        method: remove ? "DELETE" : "PUT",
        body: remove ? undefined : JSON.stringify({ key: key.trim() }),
        signal: AbortSignal.timeout(20_000),
      });
      setStatus(value);
      setKey("");
      setNotice(
        remove
          ? "Key removed. New OpenRouter requests need a key."
          : "Key verified and saved. New OpenRouter requests use it immediately.",
      );
    } catch (cause) {
      setError(`${errorMessage(cause)} Reload status before retrying.`);
    } finally {
      setBusy(false);
    }
  }
  const managed = status?.source === "deployment";
  return (
    <form
      aria-label="OpenRouter API key"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
      className="mb-8 max-w-3xl space-y-4 border-b pb-8"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-semibold">OpenRouter API key</h2>
        {status && (
          <Badge variant={status.configured ? "success" : "warning"}>
            {status.configured ? "Configured" : "Not configured"}
          </Badge>
        )}
      </div>
      <p className="text-sm leading-relaxed text-muted-foreground">
        One key for agents using OpenRouter through Talos. Choose OpenRouter and
        a model when creating an agent or in its Settings.
      </p>
      {managed ? (
        <p className="text-sm text-muted-foreground">
          Managed by your deployment. Update the mounted secret to change this
          key.
        </p>
      ) : (
        <>
          <div>
            <Label htmlFor={id} className="mb-2 block text-sm">
              {status?.configured ? "Replacement API key" : "API key"}
            </Label>
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              value={key}
              onChange={(event) => setKey(event.target.value)}
              className="w-full"
              placeholder="Paste your OpenRouter API key"
              maxLength={512}
              disabled={busy || !status}
              aria-describedby={`${id}-help`}
            />
            <p
              id={`${id}-help`}
              className="mt-2 text-xs leading-relaxed text-muted-foreground"
            >
              Saved privately on this installation. The saved key is never shown
              again or copied into agents. Saving verifies the key with
              OpenRouter without making a model request.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Button type="submit" disabled={busy || !status || !key.trim()}>
              {busy
                ? "Updating…"
                : status?.configured
                  ? "Replace key"
                  : "Verify & save key"}
            </Button>
            {status?.configured && (
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => void save(true)}
              >
                Remove key
              </Button>
            )}
          </div>
          {status?.configured && (
            <p className="text-xs text-muted-foreground">
              Replacing or removing the key affects all agents using OpenRouter
              through Talos. Requests already sent can finish with the previous
              key.
            </p>
          )}
        </>
      )}
      {!status && !error && (
        <div role="status">
          <span className="sr-only">Checking configuration…</span>
          <Skeleton className="h-8 w-40" />
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      {error && (
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          onClick={() => setReload((value) => value + 1)}
        >
          Reload status
        </Button>
      )}
      {notice && (
        <p role="status" className="text-sm text-muted-foreground">
          {notice}
        </p>
      )}
    </form>
  );
}

export function NativeModelChoice({
  value,
  onChange,
  disabled = false,
  active = true,
  onConfigure,
}: {
  value: string | null;
  onChange: (value: string | null) => void;
  disabled?: boolean;
  active?: boolean;
  onConfigure?: () => void;
}) {
  const id = useId();
  const [models, setModels] = useState<ModelCapabilities[]>([]);
  const [recommended, setRecommended] = useState("");
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const options = {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
    };
    void Promise.all([
      api<{ models: ModelCapabilities[]; recommended_model: string }>(
        "/inference/models",
        options,
      ),
      api<{ configured: boolean }>("/inference/provider", options),
    ])
      .then(([catalog, provider]) => {
        setModels(catalog.models);
        setRecommended(
          catalog.models.find((model) => model.id === catalog.recommended_model)
            ?.id ??
            catalog.models[0]?.id ??
            "",
        );
        setConfigured(provider.configured);
        setError(null);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [reload, active]);
  return (
    <fieldset disabled={disabled} className="space-y-3">
      <legend className="mb-2 text-sm font-medium">LLM provider & model</legend>
      <Label htmlFor={id} className="block text-xs text-muted-foreground">
        Provider
      </Label>
      <Select
        value={value === null ? "native" : "openrouter"}
        disabled={disabled}
        onValueChange={(provider) =>
          onChange(provider === "native" ? null : recommended)
        }
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="native">Handled by agent (default)</SelectItem>
          <SelectItem value="openrouter" disabled={!recommended}>
            OpenRouter
          </SelectItem>
        </SelectContent>
      </Select>
      {value === null ? (
        <p className="text-xs leading-relaxed text-muted-foreground">
          The agent manages its provider and model. New agents need a provider
          configured in their native workspace before they can respond.
        </p>
      ) : (
        <>
          <ModelPicker
            models={models}
            value={value}
            onChange={onChange}
            disabled={disabled}
            includeFixture={false}
          />
          <p className="break-words text-xs text-muted-foreground">
            OpenRouter · {value}
          </p>
          <a
            href="#settings"
            onClick={onConfigure}
            className="text-xs text-primary underline"
          >
            Manage OpenRouter API key
          </a>
          <p className="text-xs text-muted-foreground">
            {configured === null
              ? "Checking OpenRouter configuration…"
              : configured
                ? "Installation API key configured. Agents use it through the gateway."
                : "OpenRouter key not configured. Add a key in Talos Settings before testing."}
          </p>
        </>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}{" "}
          <Button
            type="button"
            variant="link"
            className="h-auto p-0"
            onClick={() => setReload((current) => current + 1)}
          >
            Retry model catalog
          </Button>
        </p>
      )}
    </fieldset>
  );
}

export function NativeModelSettings({
  agentId,
  modelId,
  disabled,
  onOperation,
}: {
  agentId: string;
  modelId: string | null;
  disabled: boolean;
  onOperation: (agentId: string, id: string) => void;
}) {
  const [draft, setDraft] = useState<string | null | undefined>(undefined);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [request, setRequest] = useState<{
    key: string;
    value: string | null;
  } | null>(null);
  const value = draft === undefined ? modelId : draft;
  async function save(event: FormEvent) {
    event.preventDefault();
    const next = request ?? { key: crypto.randomUUID(), value };
    setRequest(next);
    setSaving(true);
    setError(null);
    try {
      const operation = await api<{ id: string }>(
        `/inference/agents/${agentId}/native`,
        {
          method: "POST",
          headers: { "Idempotency-Key": next.key },
          body: JSON.stringify(
            next.value === null ? null : { model_id: next.value },
          ),
        },
      );
      onOperation(agentId, operation.id);
      setRequest(null);
      setDraft(undefined);
    } catch (cause) {
      setError(errorMessage(cause));
      if (cause instanceof ApiError && cause.status < 500) setRequest(null);
    } finally {
      setSaving(false);
    }
  }
  return (
    <form
      onSubmit={(event) => void save(event)}
      className="space-y-4 border-b pb-6"
    >
      <NativeModelChoice
        value={value}
        onChange={setDraft}
        disabled={disabled || saving || !!request}
      />
      <p className="text-xs leading-relaxed text-muted-foreground">
        Change the model while the agent runs. OpenRouter changes affect the
        next model request; requests already sent finish with their original
        model. Existing native sessions may retain a session-specific model
        override. “Handled by agent” restores the previous native model
        configuration.
      </p>
      <Button
        type="submit"
        disabled={disabled || saving || (draft === undefined && !request)}
      >
        {saving ? "Saving…" : request ? "Retry same request" : "Save model"}
      </Button>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </form>
  );
}

export function InferenceSettings({
  agentId,
  onSaved,
}: {
  agentId?: string;
  onSaved?: (selection: Selection) => void;
}) {
  const path = agentId ? `/inference/agents/${agentId}` : "/inference";
  const [selection, setSelection] = useState<Selection | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [models, setModels] = useState<ModelCapabilities[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const options = {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
    };
    void Promise.allSettled([
      api<Selection>(path, options)
        .then((value) => {
          if (!controller.signal.aborted) {
            setSelection(value);
            setError(null);
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(errorMessage(cause));
        }),
      api<{ models: ModelCapabilities[] }>("/inference/models", options)
        .then((value) => {
          if (!controller.signal.aborted) {
            setModels(value.models);
            setCatalogError(null);
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setCatalogError(errorMessage(cause));
        }),
    ]).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [path, reload]);

  const value = draft ?? selection;
  const inherited = !!agentId && selection?.inherited && !draft;
  const editable = !!value && !saving && !inherited;
  const model =
    models.find((item) => item.id === value?.model_id) ??
    (selection?.capabilities.id === value?.model_id
      ? (selection?.capabilities as ModelCapabilities)
      : undefined);
  function edit(next: Draft) {
    setDraft(next);
    setSaved(false);
  }

  async function save(event?: FormEvent, reset = false) {
    event?.preventDefault();
    if (!value || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api<Selection>(path, {
        method: reset ? "DELETE" : "PUT",
        body: reset
          ? undefined
          : JSON.stringify({
              model_id: value.model_id,
              settings: value.settings,
            }),
        signal: AbortSignal.timeout(15_000),
      });
      setSelection(result);
      setDraft(null);
      setSaved(true);
      onSaved?.(result);
    } catch (cause) {
      setError(
        `${errorMessage(cause)} Reload settings to check the saved configuration before retrying.`,
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void save(event)}
      aria-label={agentId ? "Agent model settings" : "Workspace model settings"}
      className="max-w-3xl"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold">
            {agentId ? "Model configuration" : "Default model"}
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
            {agentId
              ? "Inherit workspace defaults or choose a model and settings for this agent."
              : "Used by Talos-managed conversations unless the agent has a custom configuration. Native agents choose their provider and model in agent Settings."}
          </p>
        </div>
        {agentId && selection && (
          <Badge variant={inherited ? "secondary" : "default"}>
            {inherited
              ? "Workspace defaults"
              : draft && selection.inherited
                ? "Unsaved override"
                : "Custom configuration"}
          </Badge>
        )}
      </div>
      {inherited && (
        <Button
          type="button"
          variant="outline"
          className="mt-5"
          disabled={saving || loading}
          onClick={() =>
            edit({
              model_id: selection!.model_id,
              settings: { ...selection!.settings },
            })
          }
        >
          Customize for this agent
        </Button>
      )}
      <div className="mt-6 flex flex-wrap items-end gap-3">
        <ModelPicker
          models={models}
          value={value?.model_id ?? "fixture"}
          onChange={(model_id) => edit({ model_id, settings: {} })}
          disabled={!editable}
        />
      </div>
      {value?.model_id !== "fixture" && (
        <GenerationControls
          model={model}
          settings={value?.settings ?? {}}
          onChange={(settings) => edit({ model_id: value!.model_id, settings })}
          disabled={!editable}
        />
      )}
      <p
        id="model-help"
        className="mt-5 text-xs leading-relaxed text-muted-foreground"
      >
        Changes apply to new messages. Queued and running requests keep their
        original configuration.
      </p>
      {selection && (
        <p className="mt-2 break-words text-xs text-muted-foreground">
          Saved model:{" "}
          {selection.model_id === "fixture"
            ? "Local simulator"
            : selection.model_id}
        </p>
      )}
      {(error || catalogError) && (
        <p role="alert" className="mt-4 text-sm text-danger">
          {error ?? catalogError}
        </p>
      )}
      <div className="mt-6 flex flex-wrap items-center gap-3 border-t pt-5">
        {!inherited && (
          <Button type="submit" disabled={!editable}>
            {saving ? "Saving…" : "Save settings"}
          </Button>
        )}
        {agentId && !inherited && (
          <Button
            type="button"
            variant="outline"
            disabled={saving || !selection}
            onClick={() => {
              if (selection?.inherited) {
                setDraft(null);
                setSaved(false);
              } else {
                void save(undefined, true);
              }
            }}
          >
            Use workspace defaults
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          disabled={loading || saving}
          onClick={() => {
            setLoading(true);
            setSaved(false);
            setReload((current) => current + 1);
          }}
        >
          {loading ? "Loading…" : "Reload settings & models"}
        </Button>
        {saved && (
          <span role="status" className="text-sm text-muted-foreground">
            Settings saved
          </span>
        )}
      </div>
    </form>
  );
}
