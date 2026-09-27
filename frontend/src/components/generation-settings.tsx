export type GenerationSettings = {
  reasoning_effort?: string;
  max_output_tokens?: number;
  temperature?: number;
  top_p?: number;
};
export type ModelCapabilities = {
  id: string;
  name: string;
  context_length: number;
  max_completion_tokens?: number | null;
  supported_parameters: string[];
  reasoning: { mandatory?: boolean; supported_efforts?: string[] | null; default_effort?: string; default_enabled?: boolean };
};
export type InferenceSelection = { model_id: string; settings: GenerationSettings; capabilities: Partial<ModelCapabilities> };
export type InferenceCall = { model: string; outcome: string; input_tokens?: number; output_tokens?: number; reasoning_tokens?: number; cost?: number; duration_ms: number; finish_reason?: string };

export function GenerationControls({ model, settings, onChange, disabled, inputClass }: {
  model?: ModelCapabilities; settings: GenerationSettings; onChange: (settings: GenerationSettings) => void; disabled: boolean; inputClass: string;
}) {
  const supported = model?.supported_parameters ?? [];
  const reasoning = model?.reasoning;
  const efforts = reasoning?.supported_efforts ?? ["minimal", "low", "medium", "high", "xhigh", "max"];
  function update(key: keyof GenerationSettings, value: string) {
    const next = { ...settings };
    if (!value) delete next[key];
    else if (key === "reasoning_effort") next[key] = value;
    else next[key] = Number(value);
    onChange(next);
  }
  return <details className="mt-4 border-t pt-3">
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Advanced settings · {Object.keys(settings).length ? "Custom" : "Model defaults"}</summary>
    <p className="mt-3 text-xs leading-relaxed text-muted-foreground">Leave fields empty to use provider defaults. Output tokens include reasoning. Model and context limits still apply.</p>
    {model && <p className="mt-2 text-xs text-muted-foreground">Context: {model.context_length.toLocaleString()} tokens{model.max_completion_tokens ? ` · Maximum output: ${model.max_completion_tokens.toLocaleString()} tokens` : ""}{reasoning?.mandatory ? " · Reasoning required" : ""}</p>}
    <fieldset disabled={disabled || !model} className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <label className="text-xs text-muted-foreground">Reasoning effort
        <select className={`${inputClass} mt-2`} value={settings.reasoning_effort ?? ""} onChange={(e) => update("reasoning_effort", e.target.value)} disabled={!reasoning || !supported.some((p) => ["reasoning", "reasoning_effort"].includes(p))}>
          <option value="">Model default{reasoning?.default_effort ? ` (${reasoning.default_effort})` : ""}</option>
          {!reasoning?.mandatory && <option value="none">Off</option>}
          {efforts.filter((effort) => effort !== "none").map((effort) => <option key={effort} value={effort}>{effort}</option>)}
        </select>
      </label>
      <label className="text-xs text-muted-foreground">Maximum output tokens
        <input className={`${inputClass} mt-2`} type="number" min={1} max={model ? Math.min(model.max_completion_tokens ?? model.context_length, model.context_length) : undefined} step={1} placeholder="Model default" value={settings.max_output_tokens ?? ""} onChange={(e) => update("max_output_tokens", e.target.value)} disabled={!supported.some((p) => ["max_tokens", "max_completion_tokens"].includes(p))} />
      </label>
      <label className="text-xs text-muted-foreground">Temperature
        <input className={`${inputClass} mt-2`} type="number" min={0} max={2} step="any" placeholder="Model default" value={settings.temperature ?? ""} onChange={(e) => update("temperature", e.target.value)} disabled={!supported.includes("temperature")} />
      </label>
      <label className="text-xs text-muted-foreground">Top P
        <input className={`${inputClass} mt-2`} type="number" min={0.000001} max={1} step="any" placeholder="Model default" value={settings.top_p ?? ""} onChange={(e) => update("top_p", e.target.value)} disabled={!supported.includes("top_p")} />
      </label>
    </fieldset>
    <button type="button" className="mt-3 text-xs underline underline-offset-4 disabled:opacity-50" disabled={disabled || !Object.keys(settings).length} onClick={() => onChange({})}>Reset to model defaults</button>
  </details>;
}

export function UsageDetails({ calls }: { calls: InferenceCall[] }) {
  if (!calls.length) return null;
  return <div className="mt-3 space-y-2 text-xs text-muted-foreground" aria-label="Inference usage">
    {calls.map((call, i) => <p key={i} className="break-words">
      {calls.length > 1 ? `Call ${i + 1} · ` : ""}{call.outcome === "length" ? "Output limit reached" : call.outcome.replaceAll("_", " ")} · {(call.duration_ms / 1000).toFixed(1)}s
      {call.input_tokens !== undefined ? ` · ${call.input_tokens.toLocaleString()} input tokens` : ""}
      {call.output_tokens !== undefined ? ` · ${call.output_tokens.toLocaleString()} output tokens` : ""}
      {call.reasoning_tokens !== undefined ? ` (${call.reasoning_tokens.toLocaleString()} reasoning)` : ""}
      {call.cost !== undefined ? ` · $${call.cost.toFixed(6)}` : " · Cost unavailable"}
    </p>)}
  </div>;
}
