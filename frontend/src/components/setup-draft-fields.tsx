import { useEffect, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import { Plus, Trash2 } from "lucide-react";
import type { SetupConnector, SetupManifest } from "@/components/setups";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function ConnectorFields({
  connector,
  onChange,
  disabled,
}: {
  connector: SetupConnector;
  onChange: (connector: SetupConnector) => void;
  disabled: boolean;
}) {
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-xs text-muted-foreground">
        Edit connector details
      </summary>
      <div className="mt-4 space-y-4">
        <div>
          <Label htmlFor={`connector-name-${connector.id}`}>Name</Label>
          <Input
            className="mt-2"
            id={`connector-name-${connector.id}`}
            value={connector.name ?? ""}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...connector, name: event.target.value })
            }
          />
        </div>
        {connector.transport !== "stdio" && (
          <div>
            <Label htmlFor={`connector-url-${connector.id}`}>
              Endpoint URL
            </Label>
            <Input
              className="mt-2"
              id={`connector-url-${connector.id}`}
              type="url"
              value={connector.url ?? ""}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...connector, url: event.target.value })
              }
              placeholder="https://example.com/mcp"
            />
          </div>
        )}
        <div>
          <Label htmlFor={`connector-tools-${connector.id}`}>
            Declared tools
          </Label>
          <Input
            className="mt-2"
            id={`connector-tools-${connector.id}`}
            value={connector.tools.join(", ")}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...connector,
                tools: event.target.value
                  .split(",")
                  .map((value) => value.trim()),
              })
            }
            placeholder="search, create_record"
          />
          <p className="mt-2 text-xs text-muted-foreground">
            Exact tool names, separated by commas. Configure credential slots
            and local payloads in the manifest.
          </p>
        </div>
      </div>
    </details>
  );
}

export function TargetFields({
  targets,
  onChange,
  disabled,
}: {
  targets: SetupManifest["targets"];
  onChange: (targets: SetupManifest["targets"]) => void;
  disabled: boolean;
}) {
  const [available, setAvailable] = useState<
    { runtime_kind: string; runtime_release: string }[]
  >([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api<{ runtime_kind: string; runtime_release: string }[]>(
      "/setups/runtime-targets",
      { signal: controller.signal },
    )
      .then(setAvailable)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, []);
  function update(
    index: number,
    patch: Partial<SetupManifest["targets"][number]>,
  ) {
    onChange(
      targets.map((target, itemIndex) =>
        itemIndex === index ? { ...target, ...patch } : target,
      ),
    );
  }
  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-semibold">Compatibility</h2>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              size="sm"
              variant="outline"
              disabled={disabled || !available.length}
            >
              <Plus />
              Add runtime target
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {available.flatMap((runtime) =>
              ["arm64", "amd64"].map((architecture) => (
                <DropdownMenuItem
                  key={`${runtime.runtime_release}-${architecture}`}
                  disabled={targets.some(
                    (target) =>
                      target.runtime_kind === runtime.runtime_kind &&
                      target.runtime_release === runtime.runtime_release &&
                      target.architecture === architecture,
                  )}
                  onSelect={() =>
                    onChange([...targets, { ...runtime, architecture }])
                  }
                >
                  {runtime.runtime_kind === "hermes" ? "Hermes" : "OpenClaw"}
                  {" · "}
                  {runtime.runtime_release.slice(runtime.runtime_kind.length + 1)}
                  {" · "}{architecture.toUpperCase()}
                </DropdownMenuItem>
              )),
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Choose where this setup can run: Hermes, OpenClaw, or both. Capture starts
        with the source runtime. Add the other runtime to reuse the same
        instructions, skills, and connectors there. Talos translates the native
        configuration and checks compatibility when you apply.
      </p>
      {error && (
        <p role="alert" className="mt-3 text-sm text-danger">
          Runtime releases unavailable: {error}
        </p>
      )}
      <div className="mt-3 divide-y border-y">
        {targets.map((target, index) => (
          <div
            key={index}
            className="grid items-end gap-3 py-4 sm:grid-cols-[1fr_2fr_1fr_auto]"
          >
            <div>
              <Label htmlFor={`target-runtime-${index}`}>Runtime</Label>
              <Select
                disabled={disabled}
                value={target.runtime_kind}
                onValueChange={(value) =>
                  update(index, {
                    runtime_kind: value,
                    runtime_release:
                      available.find((item) => item.runtime_kind === value)
                        ?.runtime_release ?? "",
                    node_major: undefined,
                    python_version: undefined,
                  })
                }
              >
                <SelectTrigger
                  id={`target-runtime-${index}`}
                  className="mt-2 w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="hermes">Hermes</SelectItem>
                  <SelectItem value="openclaw">OpenClaw</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor={`target-release-${index}`}>Runtime release</Label>
              <Select
                value={target.runtime_release || "unselected"}
                disabled={disabled}
                onValueChange={(value) =>
                  update(index, {
                    runtime_release: value === "unselected" ? "" : value,
                  })
                }
              >
                <SelectTrigger
                  id={`target-release-${index}`}
                  className="mt-2 w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="unselected">Choose a release</SelectItem>
                  {available
                    .filter((item) => item.runtime_kind === target.runtime_kind)
                    .map((item) => (
                      <SelectItem
                        key={item.runtime_release}
                        value={item.runtime_release}
                      >
                        {item.runtime_release}
                      </SelectItem>
                    ))}
                  {target.runtime_release &&
                    !available.some(
                      (item) => item.runtime_release === target.runtime_release,
                    ) && (
                      <SelectItem value={target.runtime_release}>
                        {target.runtime_release} · unsupported
                      </SelectItem>
                    )}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor={`target-arch-${index}`}>Architecture</Label>
              <Select
                value={target.architecture}
                disabled={disabled}
                onValueChange={(value) =>
                  update(index, { architecture: value })
                }
              >
                <SelectTrigger
                  className="mt-2 w-full"
                  id={`target-arch-${index}`}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="arm64">ARM64</SelectItem>
                  <SelectItem value="amd64">AMD64</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button
              variant="ghost"
              size="icon"
              disabled={disabled}
              aria-label={`Remove runtime target ${index + 1}`}
              onClick={() =>
                onChange(targets.filter((_, itemIndex) => itemIndex !== index))
              }
            >
              <Trash2 />
            </Button>
            <details className="sm:col-span-4">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                Interpreter requirements for local connectors
              </summary>
              <p className="mt-2 text-xs text-muted-foreground">
                Pin the interpreter used by each local connector payload for this
                runtime. Hosted connectors and skills alone do not need these
                fields. Changing runtimes clears the previous interpreter pins;
                Talos does not install a different interpreter.
              </p>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div>
                  <Label htmlFor={`target-node-${index}`}>Node major</Label>
                  <Input
                    id={`target-node-${index}`}
                    className="mt-2"
                    type="number"
                    min={18}
                    max={100}
                    step={1}
                    value={typeof target.node_major === "number" ? target.node_major : ""}
                    disabled={disabled}
                    onChange={(event) =>
                      update(index, {
                        node_major: event.target.value ? Number(event.target.value) : undefined,
                      })
                    }
                  />
                </div>
                <div>
                  <Label htmlFor={`target-python-${index}`}>Python version</Label>
                  <Input
                    id={`target-python-${index}`}
                    className="mt-2"
                    placeholder="3.x"
                    value={typeof target.python_version === "string" ? target.python_version : ""}
                    disabled={disabled}
                    onChange={(event) =>
                      update(index, { python_version: event.target.value || undefined })
                    }
                  />
                </div>
              </div>
            </details>
          </div>
        ))}
      </div>
      {!targets.length && (
        <p className="mt-3 text-sm text-warning">
          Add at least one runtime target before publishing.
        </p>
      )}
    </section>
  );
}
