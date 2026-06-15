import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Loader2, Plus, ShieldAlert, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  fetchMsgvaultStatus,
  fetchSetupJob,
  runGmailAuthorize,
  runGmailVaultSetup,
  runSetupAction,
  type MsgvaultStatus,
  type SetupJob,
} from "./powerpacksApi";

// The desired account list (primary + additional) lives client-side — msgvault
// only knows who's *authorized*, not who was requested. localStorage is fine for
// a single-user local console and survives reloads.
const DESIRED_KEY = "powerpacks.gmail.desiredEmails";

function loadDesired(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(DESIRED_KEY) || "[]");
    return Array.isArray(v) ? v.map((e) => String(e).toLowerCase()).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function saveDesired(emails: string[]) {
  try {
    localStorage.setItem(DESIRED_KEY, JSON.stringify([...new Set(emails.map((e) => e.toLowerCase()).filter(Boolean))]));
  } catch {
    /* best-effort */
  }
}

async function pollJob(job: SetupJob): Promise<SetupJob> {
  let current = job;
  while (current.status === "running" || current.status === "pending") {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    current = await fetchSetupJob(job.id);
  }
  return current;
}

/**
 * Gmail vault setup, shown on the Gmail page when msgvault isn't ready yet.
 * Phase 1 (one shot): primary email (project owner) + additional emails →
 * create gcloud project + OAuth app + add all as OAuth test users (no auth).
 * Phase 2 (per account): check msgvault for who's authorized; the rest get an
 * "Authorize" button that runs the per-account browser grant. Calls onReady once
 * msgvault reports ready so the parent can swap back to stats.
 */
export function MsgvaultSetupCard({ onReady }: { onReady?: () => void }) {
  const [status, setStatus] = useState<MsgvaultStatus | null>(null);
  const [primary, setPrimary] = useState("");
  const [additional, setAdditional] = useState<string[]>([]);
  const [newEmail, setNewEmail] = useState("");
  const [busy, setBusy] = useState<string | null>(null); // "setup" | `auth:<email>`
  const [error, setError] = useState<string | null>(null);
  const [desired, setDesired] = useState<string[]>(() => loadDesired());
  const [authEmail, setAuthEmail] = useState(""); // manual "authorize another" input

  const refresh = useCallback(async () => {
    try {
      setStatus(await fetchMsgvaultStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load vault status");
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (status?.status === "ok") onReady?.();
  }, [status, onReady]);

  const loading = !status;
  const gcloudReady = Boolean(status?.gcloud?.installed && status?.gcloud?.account);
  const oauthConfigured = status?.config?.oauth_configured === true;
  const authorized = new Set(
    (status?.accounts || []).map((a) => String(a.email || "").toLowerCase()).filter(Boolean)
  );

  function addAdditional() {
    const email = newEmail.trim().toLowerCase();
    if (!email || additional.includes(email) || email === primary.trim().toLowerCase()) return;
    setAdditional((prev) => [...prev, email]);
    setNewEmail("");
  }

  async function handleCreate() {
    const primaryEmail = primary.trim().toLowerCase();
    if (!primaryEmail) return;
    setBusy("setup");
    setError(null);
    try {
      const { job } = await runGmailVaultSetup({ primaryEmail, additionalEmails: additional });
      const done = await pollJob(job);
      if (done.status !== "completed") {
        setError(done.stderr || "Vault setup didn't complete.");
      } else {
        const all = [primaryEmail, ...additional];
        saveDesired(all);
        setDesired([...new Set(all)]);
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vault setup failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleAuthorize(email: string) {
    setBusy(`auth:${email}`);
    setError(null);
    try {
      const { job } = await runGmailAuthorize({ email });
      const done = await pollJob(job);
      if (done.status !== "completed") setError(done.stderr || `Couldn't authorize ${email}.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authorization failed");
    } finally {
      setBusy(null);
    }
  }

  // Add a brand-new account: register it + add as OAuth test user, left PENDING
  // (skipAuthorize). It then appears in the list with its own Authorize button.
  // (A new email isn't a test user yet, so we can't grant it directly.)
  async function handleAddAccount(email: string) {
    const normalized = email.trim().toLowerCase();
    if (!normalized) return;
    setBusy(`add:${normalized}`);
    setError(null);
    try {
      const { job } = await runSetupAction({ action: "gmail-link-emails", emails: normalized, skipAuthorize: true });
      const done = await pollJob(job);
      if (done.status !== "completed") setError(done.stderr || `Couldn't add ${normalized}.`);
      setAuthEmail("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Add failed");
    } finally {
      setBusy(null);
    }
  }

  // gcloud prerequisite — browser-setup can't run without an authed gcloud.
  if (!loading && !gcloudReady) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldAlert className="h-4 w-4 text-amber-600" /> gcloud sign-in needed
          </CardTitle>
          <CardDescription>
            Creating your Gmail vault needs the Google Cloud CLI signed in (it owns the project).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {status?.gcloud?.installed ? (
            <p className="text-muted-foreground">
              gcloud is installed but not signed in. Run <code className="rounded bg-muted px-1">gcloud auth login</code> in
              your terminal, then this page will continue.
            </p>
          ) : (
            <p className="text-muted-foreground">
              gcloud isn&apos;t installed. Install it (<code className="rounded bg-muted px-1">brew install --cask google-cloud-sdk</code>)
              and run <code className="rounded bg-muted px-1">gcloud auth login</code>.
            </p>
          )}
        </CardContent>
      </Card>
    );
  }

  // Phase 2 — vault exists, authorize each requested account. Build the list
  // from the backend owner email + any locally-remembered desired emails + who
  // msgvault already shows as authorized, so it never depends on localStorage
  // alone (e.g. vault created in another browser/session).
  if (oauthConfigured) {
    const ownerEmail = String(status?.owner_email || "").toLowerCase();
    const backendDesired = (status?.desired_emails || []).map((e) => e.toLowerCase());
    const accounts = [...new Set(
      [ownerEmail, ...backendDesired, ...desired, ...authorized].filter(Boolean)
    )];
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Authorize your accounts</CardTitle>
          <CardDescription>
            Your vault is set up. Click Authorize for each account — a browser window opens to grant read-only Gmail access.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {accounts.length === 0 && (
            <p className="text-sm text-muted-foreground">No accounts to authorize yet.</p>
          )}
          {accounts.map((email) => {
            const isAuthorized = authorized.has(email);
            const isBusy = busy === `auth:${email}`;
            return (
              <div key={email} className="flex items-center gap-2 rounded-md border p-2 text-sm">
                <span className="break-all">{email}</span>
                {isAuthorized ? (
                  <span className="ml-auto flex items-center gap-1 text-xs text-emerald-600">
                    <CheckCircle2 className="h-4 w-4" /> Authorized
                  </span>
                ) : (
                  <Button
                    size="sm"
                    className="ml-auto"
                    disabled={busy !== null}
                    onClick={() => handleAuthorize(email)}
                  >
                    {isBusy ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                    {isBusy ? "Authorizing…" : "Authorize"}
                  </Button>
                )}
              </div>
            );
          })}
          <div className="flex items-end gap-2 pt-1">
            <Input
              value={authEmail}
              onChange={(event) => setAuthEmail(event.target.value)}
              placeholder="Add another account…"
              disabled={busy !== null}
              onKeyDown={(event) => {
                if (event.key === "Enter" && authEmail.trim()) handleAddAccount(authEmail);
              }}
            />
            <Button
              size="sm"
              variant="outline"
              disabled={busy !== null || !authEmail.trim()}
              onClick={() => handleAddAccount(authEmail)}
            >
              Add
            </Button>
          </div>
          {(busy?.startsWith("auth:") || busy?.startsWith("add:")) && (
            <p className="text-xs text-muted-foreground">A browser window opened — finish the step for this account.</p>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
    );
  }

  // Phase 1 — create the vault.
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Create your Gmail vault</CardTitle>
        <CardDescription>
          We&apos;ll create a private Google Cloud project to sync your Gmail. Enter the primary account (it owns the
          project) and any others you want to add. No message contents are read.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Primary email (project owner)</label>
          <Input
            value={primary}
            onChange={(event) => setPrimary(event.target.value)}
            placeholder="you@gmail.com"
            disabled={busy === "setup"}
          />
        </div>

        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Additional emails</label>
          {additional.map((email) => (
            <div key={email} className="flex items-center gap-2 text-sm">
              <span className="break-all">{email}</span>
              <button
                type="button"
                className="ml-auto text-muted-foreground hover:text-destructive"
                onClick={() => setAdditional((prev) => prev.filter((e) => e !== email))}
                disabled={busy === "setup"}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <div className="flex items-end gap-2">
            <Input
              value={newEmail}
              onChange={(event) => setNewEmail(event.target.value)}
              placeholder="another@gmail.com"
              disabled={busy === "setup"}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addAdditional();
                }
              }}
            />
            <Button size="sm" variant="outline" disabled={!newEmail.trim() || busy === "setup"} onClick={addAdditional}>
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <Button className="w-full" disabled={!primary.trim() || busy === "setup"} onClick={handleCreate}>
          {busy === "setup" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
          {busy === "setup" ? "Setting up — finish the browser steps…" : "Create vault"}
        </Button>
        {busy === "setup" && (
          <p className="text-xs text-muted-foreground">
            A browser window opened to create the project and OAuth app. Sign in to Google if prompted; this can take a
            minute.
          </p>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
