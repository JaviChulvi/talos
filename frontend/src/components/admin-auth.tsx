import { useCallback, useEffect, useRef, useState, type ComponentType, type FormEvent } from "react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";

type Session = { setup_required: boolean; authenticated: boolean };
const LOGOUT_KEY = "talos.auth-logout";

export function AdminAuth({
  dashboard: Dashboard,
}: {
  dashboard: ComponentType<{ onSignOut: () => void; signingOut: boolean }>;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const authenticated = session?.authenticated ?? false;

  const clear = useCallback(() => {
    generation.current++;
    setSession({ setup_required: false, authenticated: false });
    setPassword("");
    try {
      localStorage.removeItem("talos.operationIds");
    } catch {
      /* The dashboard also unmounts when browser storage is unavailable. */
    }
  }, []);

  const check = useCallback(async () => {
    const current = ++generation.current;
    try {
      const state = await api<Session>("/auth/session");
      if (current !== generation.current) return;
      if (!state.authenticated && authenticated) clear();
      setSession(state);
    } catch (cause) {
      if (current !== generation.current) return;
      clear();
      setError(errorMessage(cause));
    }
  }, [clear, authenticated]);

  useEffect(() => {
    const initial = window.setTimeout(() => void check(), 0);
    const interval = window.setInterval(() => void check(), 15_000);
    const storage = (event: StorageEvent) => {
      if (event.key === LOGOUT_KEY) clear();
    };
    const visible = () => {
      if (document.visibilityState === "visible") void check();
    };
    window.addEventListener("storage", storage);
    window.addEventListener("talos:unauthenticated", clear);
    window.addEventListener("pageshow", visible);
    document.addEventListener("visibilitychange", visible);
    return () => {
      // This is a request sequence, not a DOM ref; invalidate pending checks on cleanup.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      generation.current++;
      window.clearTimeout(initial);
      window.clearInterval(interval);
      window.removeEventListener("storage", storage);
      window.removeEventListener("talos:unauthenticated", clear);
      window.removeEventListener("pageshow", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [check, clear]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", {
        method: "POST", body: JSON.stringify({ password }), signal: AbortSignal.timeout(8000),
      });
      setPassword("");
      await check();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setError(null);
    try {
      await api("/auth/logout", { method: "POST", signal: AbortSignal.timeout(8000) });
      clear();
      try {
        localStorage.setItem(LOGOUT_KEY, crypto.randomUUID());
      } catch {
        /* Session polling still revokes other tabs if storage is unavailable. */
      }
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  if (session?.authenticated) {
    return <>
      {error && <Alert role="alert"><AlertDescription>{error}</AlertDescription></Alert>}
      <Dashboard onSignOut={() => void signOut()} signingOut={busy} />
    </>;
  }

  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <section className="w-full max-w-sm space-y-6" aria-labelledby="auth-heading">
        <div>
          <p className="mb-2 text-sm text-muted-foreground">Talos</p>
          <h1 id="auth-heading" className="text-2xl font-semibold tracking-tight">
            {!session ? "Checking access…" : session.setup_required ? "Set up administrator" : "Sign in"}
          </h1>
        </div>
        {error && <Alert role="alert"><AlertDescription>{error}</AlertDescription></Alert>}
        {session?.setup_required ? <>
          <p className="text-sm text-muted-foreground">Create the admin account from a terminal on the Talos host, then return here.</p>
          <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>docker compose exec api python -m backend.app.auth bootstrap</code></pre>
          <Button variant="outline" onClick={() => void check()}>Check again</Button>
        </> : session && <form onSubmit={(event) => void signIn(event)} className="space-y-4">
          <input type="hidden" name="username" value="admin" autoComplete="username" />
          <div className="space-y-2">
            <Label htmlFor="admin-password">Password</Label>
            <Input id="admin-password" name="password" type="password" autoComplete="current-password"
              value={password} onChange={(event) => setPassword(event.target.value)} required disabled={busy} />
          </div>
          <Button className="w-full" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
          <p className="text-sm text-muted-foreground">Forgot your password? Run this on the Talos host:</p>
          <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>docker compose exec api python -m backend.app.auth reset-password</code></pre>
        </form>}
      </section>
    </main>
  );
}
