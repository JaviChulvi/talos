import { useEffect, useState, type FormEvent } from "react";
import { CircleAlert, KeyRound, Plus, Trash2 } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

export type Connection = {
  id: string;
  name: string;
  description?: string;
  fields: string[];
  current_version_id?: string | null;
  current_version?: number | null;
};

export function Connections() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Connection | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [fields, setFields] = useState("api_key");
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [remove, setRemove] = useState<Connection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const fieldNames = [
    ...new Set(
      fields
        .split(",")
        .map((field) => field.trim())
        .filter(Boolean),
    ),
  ];

  useEffect(() => {
    const controller = new AbortController();
    api<Connection[]>("/connections", { signal: controller.signal })
      .then((data) => {
        setConnections(data);
        setLoading(false);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(cause));
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [refresh]);

  function edit(connection: Connection | null) {
    setSelected(connection);
    setName(connection?.name ?? "");
    setDescription(connection?.description ?? "");
    setFields(connection?.fields.join(", ") ?? "api_key");
    setValues({});
    setError(null);
    setOpen(true);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const connection =
        selected ??
        (await api<Connection>("/connections", {
          method: "POST",
          body: JSON.stringify({ name, description, fields: fieldNames }),
        }));
      // Preserve the new record on credential failure so retry never creates a duplicate.
      setSelected(connection);
      await api(`/connections/${connection.id}/credentials`, {
        method: "PUT",
        body: JSON.stringify({
          values: Object.fromEntries(
            fieldNames.map((field) => [field, values[field] ?? ""]),
          ),
        }),
      });
      setValues({});
      setOpen(false);
      setRefresh((value) => value + 1);
      setNotice(
        `${connection.name} credentials saved. Apply affected agents to use this version.`,
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  async function deleteConnection() {
    if (!remove) return;
    setSaving(true);
    setError(null);
    try {
      await api(`/connections/${remove.id}`, { method: "DELETE" });
      setRemove(null);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause));
      setRemove(null);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section
      className="mt-10 border-t pt-8"
      aria-labelledby="connections-heading"
    >
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="connections-heading" className="text-lg font-semibold">
            Connections
          </h2>
          <p className="mt-2 max-w-prose text-sm text-muted-foreground">
            Shared account credentials for setup tools. Roles choose defaults;
            employees can use their own connections.
          </p>
        </div>
        <Button variant="outline" onClick={() => edit(null)}>
          <Plus />
          Add connection
        </Button>
      </div>
      {error && !open && (
        <Alert className="mb-4">
          <CircleAlert />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {notice && (
        <p role="status" className="mb-4 text-sm text-success">
          {notice}
        </p>
      )}
      {loading ? (
        <Skeleton className="h-20 w-full" />
      ) : connections.length ? (
        <ul className="divide-y border-y">
          {connections.map((connection) => (
            <li
              key={connection.id}
              className="flex flex-wrap items-center gap-3 py-4"
            >
              <KeyRound
                className="size-4 text-muted-foreground"
                aria-hidden="true"
              />
              <div className="min-w-0 flex-1">
                <h3 className="break-words font-medium">{connection.name}</h3>
                <p className="mt-1 break-words text-xs text-muted-foreground">
                  {connection.fields.join(", ")}
                </p>
              </div>
              <Badge
                variant={
                  connection.current_version_id ? "secondary" : "warning"
                }
              >
                {connection.current_version_id
                  ? "Credentials saved"
                  : "Needs credentials"}
              </Badge>
              <Button
                variant="outline"
                size="sm"
                onClick={() => edit(connection)}
              >
                Rotate credentials
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Delete ${connection.name}`}
                onClick={() => setRemove(connection)}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="border-y py-6 text-sm text-muted-foreground">
          No connections yet. Add the account fields required by your setup,
          then bind the connection in Roles.
        </p>
      )}
      <Sheet
        open={open}
        onOpenChange={(value) => {
          if (!saving) {
            setOpen(value);
            if (!value) setValues({});
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>
              {selected ? `Rotate ${selected.name}` : "Add a connection"}
            </SheetTitle>
            <SheetDescription>
              Credentials are write-only. Saved values are never displayed.
              Agents keep their selected version until you apply an update.
            </SheetDescription>
          </SheetHeader>
          <form onSubmit={save} className="space-y-5 px-5 pb-8">
            {error && (
              <Alert>
                <CircleAlert />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            {!selected && (
              <>
                <div>
                  <Label htmlFor="connection-name">Name</Label>
                  <Input
                    className="mt-2"
                    id="connection-name"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    required
                    disabled={saving}
                    maxLength={120}
                    placeholder="Sales HubSpot"
                  />
                </div>
                <div>
                  <Label htmlFor="connection-description">
                    Description (optional)
                  </Label>
                  <Input
                    className="mt-2"
                    id="connection-description"
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    disabled={saving}
                  />
                </div>
                <div>
                  <Label htmlFor="connection-fields">
                    Credential field names
                  </Label>
                  <Input
                    className="mt-2"
                    id="connection-fields"
                    value={fields}
                    onChange={(event) => {
                      setFields(event.target.value);
                      setValues({});
                    }}
                    required
                    disabled={saving}
                    placeholder="api_key, account_id"
                  />
                  <p className="mt-2 text-xs text-muted-foreground">
                    Comma-separated names matching the setup’s connection slot.
                  </p>
                </div>
              </>
            )}
            {fieldNames.map((field) => (
              <div key={field}>
                <Label htmlFor={`connection-field-${field}`}>{field}</Label>
                <Input
                  className="mt-2"
                  id={`connection-field-${field}`}
                  type="password"
                  autoComplete="new-password"
                  value={values[field] ?? ""}
                  onChange={(event) =>
                    setValues((current) => ({
                      ...current,
                      [field]: event.target.value,
                    }))
                  }
                  required
                  disabled={saving}
                />
              </div>
            ))}
            <Button disabled={saving || !fieldNames.length} type="submit">
              {saving ? "Saving…" : "Save credentials"}
            </Button>
          </form>
        </SheetContent>
      </Sheet>
      <AlertDialog
        open={!!remove}
        onOpenChange={(value) => {
          if (!value) setRemove(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {remove?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Connections referenced by roles, employees, or agent applications
              cannot be deleted.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={saving}
              onClick={(event) => {
                event.preventDefault();
                void deleteConnection();
              }}
            >
              Delete connection
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
