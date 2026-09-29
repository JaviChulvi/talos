import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronRight,
  CircleAlert,
  Download,
  FileText,
  Plus,
  Upload,
  Trash2,
} from "lucide-react";
import { ConnectorFields, TargetFields } from "@/components/setup-draft-fields";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export type SetupSkill = {
  id: string;
  name?: string;
  path: string;
  enabled?: boolean;
  source?: string;
};
export type SetupConnector = {
  id: string;
  name?: string;
  enabled?: boolean;
  transport: string;
  tools: string[];
  url?: string;
  entrypoint?: string;
  [key: string]: unknown;
};
export type SetupSlot = { id: string; label?: string; fields: string[] };
export type SetupManifest = {
  schema_version: number;
  instructions: string;
  targets: {
    runtime_kind: string;
    runtime_release: string;
    architecture: string;
    [key: string]: unknown;
  }[];
  skills: SetupSkill[];
  connectors: SetupConnector[];
  connection_slots: SetupSlot[];
  assets: Record<string, string>;
  executables?: string[];
  unresolved: { kind?: string; message?: string; item_id?: string }[];
  [key: string]: unknown;
};
export type SetupRevision = {
  id: string;
  setup_id: string;
  version: number;
  manifest: SetupManifest;
  artifact_hash: string;
  created_at: string;
};
export type Setup = {
  id: string;
  name: string;
  description: string;
  draft_manifest: SetupManifest;
  draft_artifact_hash: string | null;
  revisions: SetupRevision[];
  capture_metadata?: Record<string, unknown> | null;
  updated_at: string;
};
type Validation = { valid: boolean; errors: string[] };
type ExclusionTarget = { kind: "skill" | "connector" | "plugin"; id: string };

// Review entries identify an item by ID; their kind describes the failure,
// not the entity. Never guess when IDs overlap or metadata is incomplete.
function exclusionTarget(
  draft: SetupManifest,
  metadata: Setup["capture_metadata"],
  id: string,
): ExclusionTarget | null {
  const skills = draft.skills.filter((item) => item.id === id);
  const connectors = draft.connectors.filter((item) => item.id === id);
  const kinds = new Set<ExclusionTarget["kind"]>();
  if (skills.length) kinds.add("skill");
  if (connectors.length) kinds.add("connector");
  if (Array.isArray(metadata?.candidates)) {
    for (const candidate of metadata.candidates) {
      if (
        candidate && typeof candidate === "object" && candidate.id === id &&
        ["skill", "connector", "plugin"].includes(candidate.kind)
      ) kinds.add(candidate.kind);
    }
  }
  if (kinds.size !== 1) return null;
  const kind = [...kinds][0];
  if ((kind === "skill" && skills.length !== 1) ||
      (kind === "connector" && connectors.length !== 1)) return null;
  return { kind, id };
}

function referencedSlots(connectors: SetupConnector[]): Set<string> {
  const slots = new Set<string>();
  for (const connector of connectors)
    for (const group of [connector.env, connector.headers]) {
      if (group && typeof group === "object")
        for (const value of Object.values(group))
          if (value && typeof value === "object" && "slot" in value &&
              typeof value.slot === "string") slots.add(value.slot);
    }
  return slots;
}

function excludeDraftItem(draft: SetupManifest, target: ExclusionTarget): SetupManifest {
  const roots = target.kind === "skill"
    ? draft.skills.filter((item) => item.id === target.id).map((item) => item.path)
    : target.kind === "connector" ? [`connectors/${target.id}`] : [];
  const removedConnectors = target.kind === "connector"
    ? draft.connectors.filter((item) => item.id === target.id) : [];
  const connectors = target.kind === "connector"
    ? draft.connectors.filter((item) => item.id !== target.id) : draft.connectors;
  const removedSlots = referencedSlots(removedConnectors);
  const retainedSlots = referencedSlots(connectors);
  return {
    ...draft,
    skills: target.kind === "skill"
      ? draft.skills.filter((item) => item.id !== target.id) : draft.skills,
    connectors,
    connection_slots: draft.connection_slots.filter((slot) =>
      !removedSlots.has(slot.id) || retainedSlots.has(slot.id)),
    assets: Object.fromEntries(Object.entries(draft.assets).filter(([path]) =>
      !roots.some((root) => path === root || path.startsWith(root + "/")))),
    ...(draft.executables ? { executables: draft.executables.filter((path) =>
      !roots.some((root) => path === root || path.startsWith(root + "/"))) } : {}),
    unresolved: draft.unresolved.filter((item) => item.item_id !== target.id),
  };
}

function parseDraft(text: string): SetupManifest {
  const parsed = JSON.parse(text) as SetupManifest;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    throw new Error("Use a JSON object.");
  const value = Object.assign(
    {
      instructions: "",
      targets: [],
      skills: [],
      connectors: [],
      connection_slots: [],
      assets: {},
      unresolved: [],
    },
    parsed,
  ) as SetupManifest;
  if (Array.isArray(value.connectors))
    value.connectors = value.connectors.map((connector) =>
      connector && typeof connector === "object" && !Array.isArray(connector)
        ? Object.assign({ tools: [] }, connector)
        : connector,
    );
  if (value.executables !== undefined &&
      (!Array.isArray(value.executables) || value.executables.some((path) => typeof path !== "string")))
    throw new Error("Executable paths must be a list of strings.");
  const arrays = [
    "targets",
    "skills",
    "connectors",
    "connection_slots",
    "unresolved",
  ] as const;
  for (const key of arrays)
    if (
      !Array.isArray(value[key]) ||
      value[key].some(
        (item) => !item || typeof item !== "object" || Array.isArray(item),
      )
    )
      throw new Error(`${key} must be an array of objects.`);
  if (
    typeof value.instructions !== "string" ||
    !value.assets ||
    typeof value.assets !== "object" ||
    Array.isArray(value.assets) ||
    Object.values(value.assets).some((hash) => typeof hash !== "string")
  )
    throw new Error("Use text instructions and an object of asset hashes.");
  if (
    value.skills.some(
      (item) =>
        typeof item.id !== "string" ||
        typeof item.path !== "string" ||
        typeof item.name !== "string",
    )
  )
    throw new Error("Each skill needs an id, native name, and path.");
  if (
    value.connectors.some(
      (item) =>
        typeof item.id !== "string" ||
        typeof item.name !== "string" ||
        typeof item.transport !== "string" ||
        !Array.isArray(item.tools) ||
        item.tools.some((tool) => typeof tool !== "string"),
    )
  )
    throw new Error(
      "Each connector needs an id, name, transport, and list of tool names.",
    );
  if (
    value.connection_slots.some(
      (slot) =>
        typeof slot.id !== "string" ||
        typeof slot.label !== "string" ||
        !Array.isArray(slot.fields) ||
        slot.fields.some((field) => typeof field !== "string"),
    )
  )
    throw new Error(
      "Each connection slot needs an id, label, and list of field names.",
    );
  if (
    value.targets.some(
      (target) =>
        typeof target.runtime_kind !== "string" ||
        typeof target.runtime_release !== "string" ||
        typeof target.architecture !== "string",
    )
  )
    throw new Error(
      "Each target needs a runtime kind, release, and architecture.",
    );
  if (
    value.unresolved.some(
      (item) =>
        (item.kind !== undefined && typeof item.kind !== "string") ||
        (item.message !== undefined && typeof item.message !== "string") ||
        (item.item_id !== undefined && typeof item.item_id !== "string"),
    )
  )
    throw new Error("Review messages and item identifiers must be text.");
  return value;
}

export function Setups({ setupId }: { setupId?: string }) {
  const [setups, setSetups] = useState<Setup[]>([]);
  const [draft, setDraft] = useState<SetupManifest | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [advanced, setAdvanced] = useState("");
  const [advancedError, setAdvancedError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<Validation | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [asset, setAsset] = useState<{ path: string; text: string } | null>(
    null,
  );
  const upload = useRef<HTMLInputElement>(null);
  const selected = setups.find((setup) => setup.id === setupId);
  const loadedId = useRef<string | undefined>(undefined);
  const assetRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api<Setup[]>("/setups", { signal: controller.signal })
      .then((data) => {
        setSetups(data);
        setLoading(false);
        const next = data.find((setup) => setup.id === setupId);
        if (next && loadedId.current !== setupId) {
          loadedId.current = setupId;
          try {
            setDraft(parseDraft(JSON.stringify(next.draft_manifest)));
            setAdvancedError("");
          } catch (cause) {
            setDraft({
              schema_version: 1,
              instructions: "",
              targets: [],
              skills: [],
              connectors: [],
              connection_slots: [],
              assets: {},
              unresolved: [],
            });
            setAdvancedError(errorMessage(cause));
          }
          setName(next.name);
          setDescription(next.description);
          setAdvanced(JSON.stringify(next.draft_manifest, null, 2));
          setDirty(false);
          setValidation(null);
          setError(null);
          setNotice("");
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(cause));
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [setupId, refresh]);

  useEffect(() => () => assetRequest.current?.abort(), []);

  function change(next: SetupManifest) {
    setDraft(next);
    setAdvanced(JSON.stringify(next, null, 2));
    setAdvancedError("");
    setDirty(true);
    setValidation(null);
  }

  function excludeItem(target: ExclusionTarget) {
    if (!draft) return;
    // Re-check the current draft before removing any content.
    const current = exclusionTarget(draft, selected?.capture_metadata, target.id);
    if (!current || current.kind !== target.kind) return;
    change(excludeDraftItem(draft, target));
  }

  async function create() {
    setSaving(true);
    setError(null);
    try {
      const setup = await api<Setup>("/setups", {
        method: "POST",
        body: JSON.stringify({ name: newName, description: "" }),
      });
      setSetups((current) => [...current, setup]);
      setCreateOpen(false);
      setNewName("");
      window.location.assign(`#setups/${setup.id}`);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  async function importBundle(file?: File) {
    if (!file) return;
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      const setup = await api<Setup>(
        `/setups/import${setupId ? `?setup_id=${encodeURIComponent(setupId)}` : `?name=${encodeURIComponent(file.name.replace(/\.zip$/i, ""))}`}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/zip" },
          body: file,
        },
      );
      loadedId.current = undefined;
      setRefresh((value) => value + 1);
      window.location.assign(`#setups/${setup.id}`);
      setNotice(
        "Bundle imported into the draft. Review its content, then validate and publish.",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
      if (upload.current) upload.current.value = "";
    }
  }

  async function save(publish = false) {
    if (!selected || !draft) return;
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      if (dirty) {
        await api(`/setups/${selected.id}`, {
          method: "PUT",
          body: JSON.stringify({ name, description }),
        });
        await api(`/setups/${selected.id}/draft`, {
          method: "PUT",
          body: JSON.stringify({ manifest: draft }),
        });
        setDirty(false);
      }
      const result = await api<Validation>(`/setups/${selected.id}/validation`);
      setValidation(result);
      if (publish && result.valid) {
        await api(`/setups/${selected.id}/revisions`, { method: "POST" });
        setNotice(
          "Version published. Select it on a role, then apply it to the agents you want to update.",
        );
      } else if (result.valid)
        setNotice("Draft saved and validated. Ready to publish.");
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  async function inspect(path: string) {
    if (!selected) return;
    assetRequest.current?.abort();
    const controller = new AbortController();
    assetRequest.current = controller;
    setAsset({ path, text: "Loading…" });
    try {
      const response = await fetch(
        `/api/v1/setups/${selected.id}/draft/assets?path=${encodeURIComponent(path)}`,
        { signal: controller.signal },
      );
      if (!response.ok)
        throw new Error(`Unable to read this asset (HTTP ${response.status}).`);
      const data = await response.arrayBuffer();
      const text =
        data.byteLength > 256_000
          ? "This file is too large to preview. Export the published bundle to inspect it locally."
          : new TextDecoder("utf-8", { fatal: true }).decode(data);
      if (!controller.signal.aborted) setAsset({ path, text });
    } catch (cause) {
      if (!controller.signal.aborted)
        setAsset({
          path,
          text:
            cause instanceof TypeError
              ? "Binary file. Export the published bundle to inspect it locally."
              : errorMessage(cause),
        });
    }
  }

  return (
    <section aria-label="Setup administration">
      <input
        ref={upload}
        type="file"
        accept=".zip,application/zip"
        className="sr-only"
        aria-label="Upload setup bundle"
        onChange={(event) => void importBundle(event.target.files?.[0])}
        disabled={saving}
      />
      {setupId && (
        <Button variant="ghost" asChild className="mb-5 -ml-3">
          <a href="#setups">
            <ArrowLeft />
            All setups
          </a>
        </Button>
      )}
      <div className="page-heading">
        <div>
          <h1>{selected?.name ?? "Setups"}</h1>
          <p>
            {setupId
              ? "Review a draft and publish a version agents can reproduce."
              : "Reusable instructions, skills, and tools for your roles."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={saving || (setupId !== undefined && dirty)}
            onClick={() => upload.current?.click()}
          >
            <Upload />
            {setupId ? "Replace draft bundle" : "Import ZIP"}
          </Button>
          {!setupId && (
            <Button
              onClick={() => {
                setError(null);
                setCreateOpen(true);
              }}
            >
              <Plus />
              New setup
            </Button>
          )}
        </div>
      </div>
      {error && (
        <Alert className="mb-5">
          <CircleAlert />
          <AlertDescription>
            {error}
            <Button
              variant="outline"
              size="sm"
              className="mt-2 w-fit"
              onClick={() => setRefresh((value) => value + 1)}
            >
              Refresh
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {notice && (
        <p role="status" className="mb-5 text-sm text-success">
          {notice}
        </p>
      )}
      {loading ? (
        <Skeleton className="h-40 w-full" />
      ) : !setupId ? (
        setups.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Published versions</TableHead>
                <TableHead className="hidden sm:table-cell">Updated</TableHead>
                <TableHead>
                  <span className="sr-only">Open</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {setups.map((setup) => (
                <TableRow key={setup.id}>
                  <TableCell>
                    <a
                      className="font-medium underline-offset-4 hover:underline"
                      href={`#setups/${setup.id}`}
                    >
                      {setup.name}
                    </a>
                    <p className="mt-1 max-w-prose text-xs text-muted-foreground">
                      {setup.description}
                    </p>
                  </TableCell>
                  <TableCell>
                    {setup.revisions.length ? (
                      `v${Math.max(...setup.revisions.map((revision) => revision.version))}`
                    ) : (
                      <Badge variant="secondary">Draft</Badge>
                    )}
                  </TableCell>
                  <TableCell className="hidden text-muted-foreground sm:table-cell">
                    {new Date(setup.updated_at).toLocaleDateString()}
                  </TableCell>
                  <TableCell>
                    <Button variant="ghost" size="icon" asChild>
                      <a
                        href={`#setups/${setup.id}`}
                        aria-label={`Open ${setup.name}`}
                      >
                        <ChevronRight />
                      </a>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <div className="border-y py-12">
            <h2 className="font-medium">Build once. Reuse across your team.</h2>
            <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted-foreground">
              Create a setup from a stopped agent’s Settings, import a prepared
              ZIP, or start with instructions. Publish a version before
              assigning it to a role.
            </p>
            <Button className="mt-5" variant="outline" asChild>
              <a href="#agents">
                Choose a reference agent
                <ChevronRight />
              </a>
            </Button>
          </div>
        )
      ) : !selected || !draft ? (
        <p className="text-muted-foreground">
          This setup is unavailable.{" "}
          <a className="underline" href="#setups">
            Return to setups
          </a>
          .
        </p>
      ) : (
        <Tabs defaultValue="draft">
          <TabsList className="mb-6">
            <TabsTrigger value="draft">
              Draft{dirty ? " · unsaved" : ""}
            </TabsTrigger>
            <TabsTrigger value="versions">
              Versions ({selected.revisions.length})
            </TabsTrigger>
          </TabsList>
          <TabsContent value="draft" className="space-y-8">
            <p className="text-xs text-muted-foreground">
              <a
                className="underline underline-offset-4"
                href={`/api/v1/setups/${selected.id}/draft/export`}
                download
              >
                Download draft ZIP
              </a>{" "}
              to edit bundled files, then replace the draft bundle.
            </p>
            {advancedError && (
              <Alert>
                <CircleAlert />
                <AlertDescription>
                  The draft manifest needs correction. Open Edit manifest below
                  to resolve: {advancedError}
                </AlertDescription>
              </Alert>
            )}
            <fieldset
              disabled={saving || !!advancedError}
              className="min-w-0 space-y-8"
            >
              <div className="grid gap-5 sm:grid-cols-2">
                <div>
                  <Label htmlFor="setup-name">Name</Label>
                  <Input
                    className="mt-2"
                    id="setup-name"
                    value={name}
                    disabled={saving}
                    onChange={(event) => {
                      setName(event.target.value);
                      setDirty(true);
                    }}
                    maxLength={120}
                  />
                </div>
                <div>
                  <Label htmlFor="setup-description">Description</Label>
                  <Input
                    className="mt-2"
                    id="setup-description"
                    value={description}
                    disabled={saving}
                    onChange={(event) => {
                      setDescription(event.target.value);
                      setDirty(true);
                    }}
                  />
                </div>
              </div>
              <div>
                <Label htmlFor="setup-instructions">Instructions</Label>
                <Textarea
                  id="setup-instructions"
                  className="mt-2 min-h-40"
                  value={draft.instructions ?? ""}
                  disabled={saving}
                  onChange={(event) =>
                    change({ ...draft, instructions: event.target.value })
                  }
                  placeholder="How should agents with this setup work?"
                />
              </div>
              <div className="grid gap-8 lg:grid-cols-2">
                <section>
                  <h2 className="font-semibold">Skills</h2>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Select the captured or imported skills to include.
                  </p>
                  <ul className="mt-3 divide-y border-y">
                    {(draft.skills ?? []).map((skill, index) => (
                      <li key={`${skill.id}-${index}`} className="py-3">
                        <Label className="flex items-start gap-3">
                          <Checkbox
                            className="mt-0.5"
                            checked={skill.enabled !== false}
                            disabled={saving}
                            onCheckedChange={(checked) =>
                              change({
                                ...draft,
                                skills: draft.skills.map((item, itemIndex) =>
                                  itemIndex === index
                                    ? { ...item, enabled: checked === true }
                                    : item,
                                ),
                              })
                            }
                          />
                          <span className="min-w-0">
                            <span className="block font-medium">
                              {skill.name || skill.id}
                            </span>
                            <span className="mt-1 block break-all text-xs text-muted-foreground">
                              {skill.path}
                              {skill.source ? ` · ${skill.source}` : ""}
                            </span>
                          </span>
                        </Label>
                      </li>
                    ))}
                  </ul>
                  {!draft.skills?.length && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      No skills. Import a bundle with complete skill directories
                      or capture an agent.
                    </p>
                  )}
                </section>
                <section>
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <h2 className="font-semibold">Connectors</h2>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={saving}
                      onClick={() => {
                        let number = 1;
                        while (
                          draft.connectors.some(
                            (connector) =>
                              connector.id === `connector-${number}`,
                          )
                        )
                          number++;
                        change({
                          ...draft,
                          connectors: [
                            ...draft.connectors,
                            {
                              id: `connector-${number}`,
                              name: "New connector",
                              enabled: true,
                              transport: "streamable-http",
                              tools: [],
                              url: "",
                              env: {},
                              headers: {},
                            },
                          ],
                        });
                      }}
                    >
                      <Plus />
                      Add hosted
                    </Button>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Roles choose which of these tools their agents may use.
                  </p>
                  <ul className="mt-3 divide-y border-y">
                    {(draft.connectors ?? []).map((connector, index) => (
                      <li key={`${connector.id}-${index}`} className="py-3">
                        <Label className="flex items-start gap-3">
                          <Checkbox
                            className="mt-0.5"
                            checked={connector.enabled !== false}
                            disabled={saving}
                            onCheckedChange={(checked) =>
                              change({
                                ...draft,
                                connectors: draft.connectors.map(
                                  (item, itemIndex) =>
                                    itemIndex === index
                                      ? { ...item, enabled: checked === true }
                                      : item,
                                ),
                              })
                            }
                          />
                          <span className="min-w-0">
                            <span className="block font-medium">
                              {connector.name || connector.id}
                            </span>
                            <span className="mt-1 block break-words text-xs text-muted-foreground">
                              {connector.transport} ·{" "}
                              {connector.tools?.join(", ") ||
                                "No declared tools"}
                            </span>
                          </span>
                        </Label>
                        <ConnectorFields
                          connector={connector}
                          disabled={saving}
                          onChange={(updated) =>
                            change({
                              ...draft,
                              connectors: draft.connectors.map(
                                (item, itemIndex) =>
                                  itemIndex === index ? updated : item,
                              ),
                            })
                          }
                        />
                      </li>
                    ))}
                  </ul>
                  {!draft.connectors?.length && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      No connectors. Add a hosted connector here, or import a
                      prepared local connector.
                    </p>
                  )}
                </section>
              </div>
              <section>
                <h2 className="font-semibold">Required connections</h2>
                {draft.connection_slots?.length ? (
                  <dl className="mt-3 divide-y border-y">
                    {draft.connection_slots.map((slot) => (
                      <div
                        key={slot.id}
                        className="flex flex-wrap justify-between gap-2 py-3"
                      >
                        <dt>{slot.label || slot.id}</dt>
                        <dd className="text-sm text-muted-foreground">
                          {slot.fields.join(", ")}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="mt-2 text-sm text-muted-foreground">
                    No account credentials required.
                  </p>
                )}
                <p className="mt-2 text-xs text-muted-foreground">
                  Bind actual accounts in Roles or Employees after publishing.
                  Credentials stay outside this setup.
                </p>
              </section>
              <TargetFields
                targets={draft.targets ?? []}
                disabled={saving}
                onChange={(targets) => change({ ...draft, targets })}
              />
              <section>
                <h2 className="font-semibold">Files</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  Inspect imported assets. Replace the bundle to update file
                  contents.
                </p>
                <ul className="mt-3 max-h-64 overflow-y-auto divide-y border-y">
                  {Object.entries(draft.assets ?? {}).map(([path, hash]) => (
                    <li key={path}>
                      <Button
                        className="h-auto w-full justify-start py-3 text-left"
                        variant="ghost"
                        onClick={() => void inspect(path)}
                      >
                        <FileText className="shrink-0" />
                        <span className="min-w-0">
                          <span className="block break-all whitespace-normal">
                            {path}
                          </span>
                          <span className="mt-1 block truncate font-mono text-xs text-muted-foreground">
                            {hash}
                          </span>
                        </span>
                      </Button>
                    </li>
                  ))}
                </ul>
                {!Object.keys(draft.assets ?? {}).length && (
                  <p className="mt-3 text-sm text-muted-foreground">
                    No bundled files.
                  </p>
                )}
              </section>
            </fieldset>
            <details className="border-y py-4">
              <summary className="cursor-pointer font-medium">
                Edit manifest
              </summary>
              <p className="my-3 text-sm text-muted-foreground">
                Configure runtime targets, connector endpoints, declared tools,
                connection slots, and dependency provenance. Credential values
                belong in Connections.
              </p>
              <Label htmlFor="setup-manifest" className="sr-only">
                Setup manifest JSON
              </Label>
              <Textarea
                id="setup-manifest"
                className="min-h-80 font-mono text-xs"
                spellCheck={false}
                value={advanced}
                disabled={saving}
                onChange={(event) => {
                  setAdvanced(event.target.value);
                  setDirty(true);
                  setValidation(null);
                  try {
                    const parsed = parseDraft(event.target.value);
                    setDraft(parsed);
                    setAdvancedError("");
                  } catch (cause) {
                    setAdvancedError(errorMessage(cause));
                  }
                }}
              />
              {advancedError && (
                <p role="alert" className="mt-2 text-sm text-danger">
                  {advancedError}
                </p>
              )}
            </details>
            {!!draft.unresolved?.length && (
              <Alert>
                <CircleAlert />
                <AlertDescription>
                  <p className="font-medium">Captured items need review</p>
                  <ul className="mt-2 list-disc space-y-1 pl-5">
                    {draft.unresolved.map((item, index) => {
                      const target = item.item_id
                        ? exclusionTarget(draft, selected.capture_metadata, item.item_id)
                        : null;
                      return (
                        <li key={index} className="break-words">
                          {item.message || item.kind || "Captured item requires review"}
                          {target ? (
                            <Button
                              size="sm"
                              variant="outline"
                              className="ml-2"
                              disabled={saving || !!advancedError}
                              onClick={() => excludeItem(target)}
                            >
                              <Trash2 />
                              Exclude {target.kind} {target.id}
                            </Button>
                          ) : item.item_id ? (
                            <span className="mt-1 block text-xs">
                              Item {item.item_id} cannot be identified uniquely.
                              Resolve it in Edit manifest.
                            </span>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                  <p className="mt-2">
                    Exclude unsupported items here, or replace them with
                    prepared artifacts and clear the resolved review entries in
                    the manifest.
                  </p>
                </AlertDescription>
              </Alert>
            )}
            {validation && !validation.valid && (
              <Alert>
                <CircleAlert />
                <AlertDescription>
                  <p className="font-medium">
                    Resolve these issues before publishing
                  </p>
                  <ul className="mt-2 list-disc space-y-1 pl-5">
                    {validation.errors.map((message, index) => (
                      <li key={index}>{message}</li>
                    ))}
                  </ul>
                </AlertDescription>
              </Alert>
            )}
            <div className="flex flex-wrap gap-3 border-t pt-5">
              <Button
                variant="outline"
                disabled={saving || !!advancedError || !name.trim()}
                onClick={() => void save()}
              >
                Save and validate
              </Button>
              {dirty && (
                <Button
                  variant="ghost"
                  disabled={saving}
                  onClick={() => {
                    try {
                      setDraft(parseDraft(JSON.stringify(selected.draft_manifest)));
                      setAdvancedError("");
                    } catch (cause) {
                      setAdvancedError(errorMessage(cause));
                    }
                    setAdvanced(
                      JSON.stringify(selected.draft_manifest, null, 2),
                    );
                    setName(selected.name);
                    setDescription(selected.description);
                    setDirty(false);
                    setValidation(null);
                  }}
                >
                  Discard changes
                </Button>
              )}
              <Button
                disabled={saving || !!advancedError || !name.trim()}
                onClick={() => void save(true)}
              >
                {saving ? "Saving…" : "Publish version"}
              </Button>
              <p className="w-full text-xs text-muted-foreground">
                Published versions are immutable. Existing agents only change
                when you explicitly apply an update.
              </p>
            </div>
          </TabsContent>
          <TabsContent value="versions">
            {selected.revisions.length ? (
              <ul className="divide-y border-y">
                {[...selected.revisions]
                  .sort((a, b) => b.version - a.version)
                  .map((revision) => (
                    <li
                      className="flex flex-wrap items-center gap-4 py-5"
                      key={revision.id}
                    >
                      <div className="min-w-0 flex-1">
                        <h2 className="font-medium">
                          Version {revision.version}
                        </h2>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {new Date(revision.created_at).toLocaleString()} ·{" "}
                          {revision.manifest.skills?.length ?? 0} skills ·{" "}
                          {revision.manifest.connectors?.length ?? 0} connectors
                        </p>
                        <p
                          className="mt-2 truncate font-mono text-xs text-muted-foreground"
                          title={revision.artifact_hash}
                        >
                          {revision.artifact_hash}
                        </p>
                      </div>
                      <Button variant="outline" asChild>
                        <a
                          href={`/api/v1/setups/${selected.id}/revisions/${revision.id}/export`}
                          download
                        >
                          <Download />
                          Export ZIP
                        </a>
                      </Button>
                    </li>
                  ))}
              </ul>
            ) : (
              <p className="py-8 text-sm text-muted-foreground">
                No published versions. Validate your draft, then publish the
                first version.
              </p>
            )}
          </TabsContent>
        </Tabs>
      )}
      <Dialog
        open={createOpen}
        onOpenChange={(value) => {
          if (!saving) setCreateOpen(value);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New setup</DialogTitle>
            <DialogDescription>
              Start a draft with instructions, then import skills and tools or
              edit its manifest.
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <div>
              <Label htmlFor="new-setup-name">Name</Label>
              <Input
                className="mt-2"
                id="new-setup-name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                required
                maxLength={120}
                placeholder="Sales"
                autoFocus
                disabled={saving}
              />
            </div>
            {error && (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
            <Button type="submit" disabled={saving || !newName.trim()}>
              {saving ? "Creating…" : "Create draft"}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!asset}
        onOpenChange={(open) => {
          if (!open) {
            assetRequest.current?.abort();
            setAsset(null);
          }
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-hidden sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="break-all">{asset?.path}</DialogTitle>
            <DialogDescription>Draft asset preview</DialogDescription>
          </DialogHeader>
          <pre className="max-h-[60dvh] overflow-auto rounded-md bg-muted p-4 text-xs leading-relaxed">
            {asset?.text}
          </pre>
        </DialogContent>
      </Dialog>
    </section>
  );
}
