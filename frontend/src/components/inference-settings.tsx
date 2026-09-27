import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { GenerationControls, type GenerationSettings, type InferenceSelection, type ModelCapabilities } from "@/components/generation-settings";
import { ModelPicker } from "@/components/model-picker";
import { api, errorMessage } from "@/lib/api";

type Selection = InferenceSelection & { inherited?: boolean };
type Draft = { model_id: string; settings: GenerationSettings };
const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

export function InferenceSettings({ agentId, onSaved }: { agentId?: string; onSaved?: (selection: Selection) => void }) {
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
    const options = { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]) };
    void Promise.allSettled([
      api<Selection>(path, options).then((value) => { if (!controller.signal.aborted) { setSelection(value); setError(null); } }).catch((cause) => { if (!controller.signal.aborted) setError(errorMessage(cause)); }),
      api<{ models: ModelCapabilities[] }>("/inference/models", options).then((value) => { if (!controller.signal.aborted) { setModels(value.models); setCatalogError(null); } }).catch((cause) => { if (!controller.signal.aborted) setCatalogError(errorMessage(cause)); }),
    ]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, reload]);

  const value = draft ?? selection;
  const inherited = !!agentId && selection?.inherited && !draft;
  const editable = !!value && !saving && !inherited;
  const model = models.find((item) => item.id === value?.model_id) ?? (selection?.capabilities.id === value?.model_id ? selection?.capabilities as ModelCapabilities : undefined);
  function edit(next: Draft) { setDraft(next); setSaved(false); }

  async function save(event?: FormEvent, reset = false) {
    event?.preventDefault();
    if (!value || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api<Selection>(path, {
        method: reset ? "DELETE" : "PUT",
        body: reset ? undefined : JSON.stringify({ model_id: value.model_id, settings: value.settings }),
        signal: AbortSignal.timeout(15_000),
      });
      setSelection(result);
      setDraft(null);
      setSaved(true);
      onSaved?.(result);
    } catch (cause) { setError(`${errorMessage(cause)} Reload settings to check the saved configuration before retrying.`); }
    finally { setSaving(false); }
  }

  return <form onSubmit={(event) => void save(event)} aria-label={agentId ? "Agent model settings" : "Workspace model settings"} className="max-w-3xl">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="font-semibold">{agentId ? "Model configuration" : "Default model"}</h2><p className="mt-2 text-sm leading-relaxed text-muted-foreground">{agentId ? "Inherit workspace defaults or choose a model and settings for this agent." : "Used by all agents unless they have a custom configuration."}</p></div>
      {agentId && selection && <Badge variant={inherited ? "secondary" : "default"}>{inherited ? "Workspace defaults" : draft && selection.inherited ? "Unsaved override" : "Custom configuration"}</Badge>}
    </div>
    {inherited && <Button type="button" variant="outline" className="mt-5" disabled={saving || loading} onClick={() => edit({ model_id: selection!.model_id, settings: { ...selection!.settings } })}>Customize for this agent</Button>}
    <div className="mt-6 flex flex-wrap items-end gap-3">
      <ModelPicker models={models} value={value?.model_id ?? "fixture"} onChange={(model_id) => edit({ model_id, settings: {} })} disabled={!editable} inputClass={inputClass} />
    </div>
    {value?.model_id !== "fixture" && <GenerationControls model={model} settings={value?.settings ?? {}} onChange={(settings) => edit({ model_id: value!.model_id, settings })} disabled={!editable} inputClass={inputClass} />}
    <p id="model-help" className="mt-5 text-xs leading-relaxed text-muted-foreground">Changes apply to new messages. Queued and running requests keep their original configuration.</p>
    {selection && <p className="mt-2 break-words text-xs text-muted-foreground">Saved model: {selection.model_id === "fixture" ? "Local simulator" : selection.model_id}</p>}
    {(error || catalogError) && <p role="alert" className="mt-4 text-sm text-danger">{error ?? catalogError}</p>}
    <div className="mt-6 flex flex-wrap items-center gap-3 border-t pt-5">
      {!inherited && <Button type="submit" disabled={!editable}>{saving ? "Saving…" : "Save settings"}</Button>}
      {agentId && !inherited && <Button type="button" variant="outline" disabled={saving || !selection} onClick={() => { if (selection?.inherited) { setDraft(null); setSaved(false); } else { void save(undefined, true); } }}>Use workspace defaults</Button>}
      <Button type="button" variant="outline" disabled={loading || saving} onClick={() => { setLoading(true); setSaved(false); setReload((current) => current + 1); }}>{loading ? "Loading…" : "Reload settings & models"}</Button>
      {saved && <span role="status" className="text-sm text-muted-foreground">Settings saved</span>}
    </div>
  </form>;
}
