import { useEffect, useRef } from "react";
import { ChevronDown, FlaskConical } from "lucide-react";
import { cn } from "@/lib/utils";

type Model = { id: string; name: string };
const labIcons: Record<string, string> = {
  "aion-labs": "aionlabs", amazon: "aws", anthropic: "anthropic", "arcee-ai": "arcee",
  baidu: "baidu", bytedance: "bytedance", "bytedance-seed": "bytedance", cohere: "cohere",
  deepseek: "deepseek", "dots-studio": "dotsstudio", fireworks: "fireworks", google: "google",
  "ibm-granite": "ibm", inception: "inception", "inference-net": "inference", kwaipilot: "kwaipilot",
  liquid: "liquid", meituan: "longcat", meta: "meta", minimax: "minimax", mistralai: "mistral",
  moonshotai: "moonshot", morph: "morph", nvidia: "nvidia", nousresearch: "nousresearch",
  openai: "openai", perceptron: "perceptron", perplexity: "perplexity", poolside: "poolside",
  qwen: "qwen", rekaai: "reka", relace: "relace", "x-ai": "xai", stepfun: "stepfun",
  tencent: "tencent", upstage: "upstage", cognitivecomputations: "venice", microsoft: "microsoft",
  xiaomi: "xiaomimimo", "z-ai": "zai", inclusionai: "antgroup",
};
const labNames: Record<string, string> = {
  fixture: "Local simulator", meta: "Meta", mistralai: "Mistral", qwen: "Qwen", rekaai: "Reka",
  microsoft: "Microsoft", "anthracite-org": "Anthracite", unbiased: "Unbiased", stealth: "Stealth",
};

function labId(modelId: string) {
  const prefix = modelId.split("/")[0];
  return prefix === "meta-llama" ? "meta" : prefix;
}

function LabIcon({ id, name }: { id: string; name: string }) {
  if (id === "fixture") return <FlaskConical className="size-5" aria-hidden="true" />;
  const icon = labIcons[id];
  return icon
    ? <span aria-hidden="true" className="block size-6 shrink-0 bg-current" style={{ mask: `url(/lab-icons/${icon}.svg) center / contain no-repeat` }} />
    : <span aria-hidden="true" className="text-xs font-semibold uppercase">{name.slice(0, 2)}</span>;
}

export function ModelPicker({ models, value, onChange, disabled, inputClass }: {
  models: Model[]; value: string; onChange: (id: string) => void; disabled: boolean; inputClass: string;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  const catalog = [{ id: "fixture", name: "Local simulator" }, ...models];
  if (!catalog.some((model) => model.id === value)) catalog.push({ id: value, name: value });
  const labs = Array.from(new Set(catalog.map((model) => labId(model.id)))).map((id) => {
    const first = catalog.find((model) => labId(model.id) === id)!;
    const name = labNames[id] ?? (first.name.includes(": ") ? first.name.split(": ")[0] : id);
    return { id, name };
  }).sort((a, b) => a.name.localeCompare(b.name));
  const selectedLab = labs.find((lab) => lab.id === labId(value))!;
  const visibleModels = catalog.filter((model) => labId(model.id) === selectedLab.id);

  useEffect(() => {
    function closeOutside(event: PointerEvent) {
      if (event.target instanceof Node && !details.current?.contains(event.target)) details.current?.removeAttribute("open");
    }
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, []);

  return <div className="flex min-w-0 flex-1 basis-72 items-end gap-3">
    <div className="shrink-0">
      <span className="mb-2 block text-xs text-muted-foreground">Lab</span>
      <details ref={details} className="relative" onKeyDown={(event) => {
        if (event.key === "Escape") { details.current?.removeAttribute("open"); details.current?.querySelector("summary")?.focus(); }
      }}>
        <summary aria-label={`Choose lab: ${selectedLab.name}`} aria-disabled={disabled} title={selectedLab.name}
          onClick={(event) => { if (disabled) event.preventDefault(); }}
          className={cn("flex h-10 cursor-pointer list-none items-center gap-3 rounded-md border border-input bg-background px-3 hover:bg-muted [&::-webkit-details-marker]:hidden", disabled && "cursor-default opacity-50")}>
          <LabIcon id={selectedLab.id} name={selectedLab.name} /><ChevronDown className="size-3 text-muted-foreground" aria-hidden="true" />
        </summary>
        <div className="absolute left-0 top-full z-40 mt-2 w-72 max-w-[calc(100vw-3rem)] rounded-md border bg-panel p-3" role="group" aria-label="Labs">
          <p className="mb-3 text-xs text-muted-foreground">Choose a lab</p>
          <div className="grid max-h-64 grid-cols-5 gap-2 overflow-y-auto p-1">
            {labs.map((lab) => <button key={lab.id} type="button" title={lab.name} aria-label={lab.name} aria-pressed={lab.id === selectedLab.id} disabled={disabled}
              className={cn("flex size-10 items-center justify-center rounded-md hover:bg-muted disabled:opacity-50", lab.id === selectedLab.id && "bg-muted text-primary ring-1 ring-primary")}
              onClick={() => {
                onChange(lab.id === selectedLab.id ? value : catalog.find((model) => labId(model.id) === lab.id)!.id);
                details.current?.removeAttribute("open"); details.current?.querySelector("summary")?.focus();
              }}><LabIcon id={lab.id} name={lab.name} /></button>)}
          </div>
        </div>
      </details>
    </div>
    <div className="min-w-0 flex-1">
      <label htmlFor="default-model" className="mb-2 block text-xs text-muted-foreground">Model</label>
      <select id="default-model" className={cn(inputClass, "h-10")} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled} aria-describedby="model-help">
        {visibleModels.map((model) => <option key={model.id} value={model.id}>{model.name.replace(/^[^:]+: /, "")}</option>)}
      </select>
    </div>
  </div>;
}
