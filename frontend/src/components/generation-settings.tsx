import { useId } from "react";
import { ChevronDown } from "lucide-react";
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
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

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
  reasoning: {
    mandatory?: boolean;
    supported_efforts?: string[] | null;
    default_effort?: string;
    default_enabled?: boolean;
  };
};
export type InferenceSelection = {
  model_id: string;
  settings: GenerationSettings;
  capabilities: Partial<ModelCapabilities>;
};
export type InferenceCall = {
  model: string;
  outcome: string;
  input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  cost?: number;
  duration_ms: number;
  finish_reason?: string;
};

export function GenerationControls({
  model,
  settings,
  onChange,
  disabled,
}: {
  model?: ModelCapabilities;
  settings: GenerationSettings;
  onChange: (settings: GenerationSettings) => void;
  disabled: boolean;
}) {
  const id = useId();
  const supported = model?.supported_parameters ?? [];
  const reasoning = model?.reasoning;
  const efforts = reasoning?.supported_efforts ?? [
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
  ];
  function update(key: keyof GenerationSettings, value: string) {
    const next = { ...settings };
    if (!value) delete next[key];
    else if (key === "reasoning_effort") next[key] = value;
    else next[key] = Number(value);
    onChange(next);
  }
  return (
    <Collapsible className="mt-4 border-t pt-3">
      <CollapsibleTrigger asChild>
        <Button type="button" variant="ghost" className="-ml-3">
          Advanced settings ·{" "}
          {Object.keys(settings).length ? "Custom" : "Model defaults"}
          <ChevronDown className="size-3" />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
          Leave fields empty to use provider defaults. Output tokens include
          reasoning. Model and context limits still apply.
        </p>
        {model && (
          <p className="mt-2 text-xs text-muted-foreground">
            Context: {model.context_length.toLocaleString()} tokens
            {model.max_completion_tokens
              ? ` · Maximum output: ${model.max_completion_tokens.toLocaleString()} tokens`
              : ""}
            {reasoning?.mandatory ? " · Reasoning required" : ""}
          </p>
        )}
        <fieldset
          disabled={disabled || !model}
          className="mt-4 grid gap-4 sm:grid-cols-2"
        >
          <div className="space-y-2">
            <Label htmlFor={`${id}-reasoning`}>Reasoning effort</Label>
            <Select
              value={settings.reasoning_effort ?? "default"}
              onValueChange={(value) =>
                update("reasoning_effort", value === "default" ? "" : value)
              }
              disabled={
                disabled ||
                !model ||
                !reasoning ||
                !supported.some((p) =>
                  ["reasoning", "reasoning_effort"].includes(p),
                )
              }
            >
              <SelectTrigger id={`${id}-reasoning`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">
                  Model default
                  {reasoning?.default_effort
                    ? ` (${reasoning.default_effort})`
                    : ""}
                </SelectItem>
                {!reasoning?.mandatory && (
                  <SelectItem value="none">Off</SelectItem>
                )}
                {efforts
                  .filter((effort) => effort !== "none")
                  .map((effort) => (
                    <SelectItem key={effort} value={effort}>
                      {effort}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
          <Label className="block text-xs text-muted-foreground">
            Maximum output tokens
            <Input
              className="mt-2"
              type="number"
              min={1}
              max={
                model
                  ? Math.min(
                      model.max_completion_tokens ?? model.context_length,
                      model.context_length,
                    )
                  : undefined
              }
              step={1}
              placeholder="Model default"
              value={settings.max_output_tokens ?? ""}
              onChange={(e) => update("max_output_tokens", e.target.value)}
              disabled={
                !supported.some((p) =>
                  ["max_tokens", "max_completion_tokens"].includes(p),
                )
              }
            />
          </Label>
          <Label className="block text-xs text-muted-foreground">
            Temperature
            <Input
              className="mt-2"
              type="number"
              min={0}
              max={2}
              step="any"
              placeholder="Model default"
              value={settings.temperature ?? ""}
              onChange={(e) => update("temperature", e.target.value)}
              disabled={!supported.includes("temperature")}
            />
          </Label>
          <Label className="block text-xs text-muted-foreground">
            Top P
            <Input
              className="mt-2"
              type="number"
              min={0.000001}
              max={1}
              step="any"
              placeholder="Model default"
              value={settings.top_p ?? ""}
              onChange={(e) => update("top_p", e.target.value)}
              disabled={!supported.includes("top_p")}
            />
          </Label>
        </fieldset>
        <Button
          type="button"
          variant="link"
          className="mt-3 h-auto p-0 text-xs"
          disabled={disabled || !Object.keys(settings).length}
          onClick={() => onChange({})}
        >
          Reset to model defaults
        </Button>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function UsageDetails({ calls }: { calls: InferenceCall[] }) {
  if (!calls.length) return null;
  return (
    <div
      className="mt-3 space-y-2 text-xs text-muted-foreground"
      aria-label="Inference usage"
    >
      {calls.map((call, i) => (
        <p key={i} className="break-words">
          {calls.length > 1 ? `Call ${i + 1} · ` : ""}
          {call.outcome === "length"
            ? "Output limit reached"
            : call.outcome.replaceAll("_", " ")}{" "}
          · {(call.duration_ms / 1000).toFixed(1)}s
          {call.input_tokens !== undefined
            ? ` · ${call.input_tokens.toLocaleString()} input tokens`
            : ""}
          {call.output_tokens !== undefined
            ? ` · ${call.output_tokens.toLocaleString()} output tokens`
            : ""}
          {call.reasoning_tokens !== undefined
            ? ` (${call.reasoning_tokens.toLocaleString()} reasoning)`
            : ""}
          {call.cost !== undefined
            ? ` · $${call.cost.toFixed(6)}`
            : " · Cost unavailable"}
        </p>
      ))}
    </div>
  );
}
