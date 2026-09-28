import { useEffect, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import { Plus, Trash2 } from "lucide-react";
import type { SetupConnector, SetupManifest } from "@/components/setups";
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
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() =>
            onChange([
              ...targets,
              {
                runtime_kind: available[0]?.runtime_kind ?? "hermes",
                runtime_release: available[0]?.runtime_release ?? "",
                architecture: "arm64",
              },
            ])
          }
        >
          <Plus />
          Add runtime target
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Pin each runtime release and architecture this setup supports. Capture
        supplies the source agent’s versions automatically.
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
