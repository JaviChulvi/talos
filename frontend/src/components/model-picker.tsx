import { useId, useState } from "react";
import { Check, ChevronsUpDown, FlaskConical } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { cn } from "@/lib/utils";

type Model = { id: string; name: string };
const labIcons: Record<string, string> = {
  "aion-labs": "aionlabs",
  amazon: "aws",
  anthropic: "anthropic",
  "arcee-ai": "arcee",
  baidu: "baidu",
  bytedance: "bytedance",
  "bytedance-seed": "bytedance",
  cohere: "cohere",
  deepseek: "deepseek",
  "dots-studio": "dotsstudio",
  fireworks: "fireworks",
  google: "google",
  "ibm-granite": "ibm",
  inception: "inception",
  "inference-net": "inference",
  kwaipilot: "kwaipilot",
  liquid: "liquid",
  meituan: "longcat",
  meta: "meta",
  minimax: "minimax",
  mistralai: "mistral",
  moonshotai: "moonshot",
  morph: "morph",
  nvidia: "nvidia",
  nousresearch: "nousresearch",
  openai: "openai",
  perceptron: "perceptron",
  perplexity: "perplexity",
  poolside: "poolside",
  qwen: "qwen",
  rekaai: "reka",
  relace: "relace",
  "x-ai": "xai",
  stepfun: "stepfun",
  tencent: "tencent",
  upstage: "upstage",
  cognitivecomputations: "venice",
  microsoft: "microsoft",
  xiaomi: "xiaomimimo",
  "z-ai": "zai",
  inclusionai: "antgroup",
};
const labNames: Record<string, string> = {
  fixture: "Local simulator",
  meta: "Meta",
  mistralai: "Mistral",
  qwen: "Qwen",
  rekaai: "Reka",
  microsoft: "Microsoft",
  "anthracite-org": "Anthracite",
  unbiased: "Unbiased",
  stealth: "Stealth",
};

function labId(modelId: string) {
  const prefix = modelId.split("/")[0];
  return prefix === "meta-llama" ? "meta" : prefix;
}

function LabIcon({ id, name }: { id: string; name: string }) {
  if (id === "fixture")
    return <FlaskConical className="size-5" aria-hidden="true" />;
  const icon = labIcons[id];
  return icon ? (
    <span
      aria-hidden="true"
      className="block size-6 shrink-0 bg-current"
      style={{ mask: `url(/lab-icons/${icon}.svg) center / contain no-repeat` }}
    />
  ) : (
    <span aria-hidden="true" className="text-xs font-semibold uppercase">
      {name.slice(0, 2)}
    </span>
  );
}

export function ModelPicker({
  models,
  value,
  onChange,
  disabled,
  includeFixture = true,
}: {
  models: Model[];
  value: string;
  onChange: (id: string) => void;
  disabled: boolean;
  includeFixture?: boolean;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const catalog = [
    ...(includeFixture ? [{ id: "fixture", name: "Local simulator" }] : []),
    ...models,
  ];
  if (value && !catalog.some((model) => model.id === value))
    catalog.push({ id: value, name: value });
  const selected = catalog.find((model) => model.id === value);
  const labs = Array.from(
    new Set(catalog.map((model) => labId(model.id))),
  ).sort();
  return (
    <div className="min-w-0 flex-1 space-y-2">
      <Label htmlFor={id}>Model</Label>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            variant="outline"
            role="combobox"
            aria-expanded={open}
            disabled={disabled}
            className="h-11 w-full min-w-0 justify-between font-normal"
          >
            <span className="flex min-w-0 items-center gap-2">
              {selected && <LabIcon id={labId(value)} name={selected.name} />}
              <span className="truncate">
                {selected?.name ?? "Choose a model"}
              </span>
            </span>
            <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          className="w-[var(--radix-popover-trigger-width)] min-w-64 max-w-[calc(100vw-2rem)] p-0"
          align="start"
        >
          <Command>
            <CommandInput placeholder="Search models or providers…" />
            <CommandList>
              <CommandEmpty>No models found.</CommandEmpty>
              {labs.map((lab) => (
                <CommandGroup key={lab} heading={labNames[lab] ?? lab}>
                  {catalog
                    .filter((model) => labId(model.id) === lab)
                    .map((model) => (
                      <CommandItem
                        key={model.id}
                        value={`${model.name} ${model.id}`}
                        onSelect={() => {
                          if (model.id !== value) onChange(model.id);
                          setOpen(false);
                        }}
                      >
                        <LabIcon id={lab} name={model.name} />
                        <span className="min-w-0 flex-1 break-words">
                          {model.name}
                        </span>
                        <Check
                          className={cn(
                            "size-4",
                            value === model.id ? "opacity-100" : "opacity-0",
                          )}
                        />
                      </CommandItem>
                    ))}
                </CommandGroup>
              ))}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}
