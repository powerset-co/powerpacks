import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Clock, Loader2, Mail, MessageSquare, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  estimateGmailSync,
  fetchGmailAccounts,
  fetchMsgvaultStatus,
  fetchSetupJob,
  runGmailAuthorize,
  runGmailWindowSync,
  runSetupAction,
  type GmailAccount,
  type GmailSyncEstimateResponse,
  type GmailSyncWindowEstimate,
} from "./powerpacksApi";

export const SYNC_WINDOWS = [
  { id: "1y", label: "1 year" },
  { id: "2y", label: "2 years" },
  { id: "5y", label: "5 years" },
  { id: "all", label: "All time" },
] as const;
export type SyncWindowId = (typeof SYNC_WINDOWS)[number]["id"];

/**
 * Gmail account list + date-window estimate + windowed sync. msgvault is the
 * single source of truth for accounts. Used by onboarding and the Gmail source
 * page. Calls onChange after a sync so a parent can refresh its own stats.
 */
export function GmailSyncPanel({ onChange }: { onChange?: () => void } = {}) {
  const [accounts, setAccounts] = useState<GmailAccount[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [totals, setTotals] = useState<Record<string, GmailSyncWindowEstimate>>({});
  const [selected, setSelected] = useState<SyncWindowId>("1y");
  const [estimating, setEstimating] = useState(false);
  const [allPending, setAllPending] = useState(false);
  const [linking, setLinking] = useState(false);
  const [linkingEmail, setLinkingEmail] = useState<string | null>(null); // which email is being authorized
  const [addOpen, setAddOpen] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [desired, setDesired] = useState<string[]>([]); // emails the user asked to authorize (backend test_users)
  const [syncing, setSyncing] = useState(false);
  const [syncDone, setSyncDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runEstimate = useCallback((emails: string[]) => {
    if (emails.length === 0) {
      setTotals({});
      return;
    }
    setEstimating(true);
    setAllPending(true);
    const merge = (res: GmailSyncEstimateResponse) => {
      if (res.status === "completed" && res.totals) setTotals((prev) => ({ ...prev, ...res.totals }));
    };
    // Fast windows first so the panel fills in quickly; "all time" pagination
    // can take ~30s on a large inbox, so fetch it in parallel without blocking.
    estimateGmailSync({ accounts: emails, windows: ["1y", "2y", "5y"] })
      .then((res) => {
        merge(res);
        if (res.status !== "completed") setError(res.error || "Couldn't estimate your inbox.");
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Estimate failed"))
      .finally(() => setEstimating(false));
    estimateGmailSync({ accounts: emails, windows: ["all"] })
      .then(merge)
      .catch(() => {})
      .finally(() => setAllPending(false));
  }, []);

  // msgvault is the single source of truth for which Gmail accounts exist.
  const loadAccounts = useCallback(async () => {
    try {
      const res = await fetchGmailAccounts();
      const next = res.status === "completed" ? res.accounts : [];
      setAccounts(next);
      if (res.status !== "completed") setError(res.error || "Failed to load Gmail accounts");
      return next.map((account) => account.email);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load Gmail accounts");
      return [];
    } finally {
      setAccountsLoading(false);
    }
  }, []);

  // Desired emails (test_users) are the backend source of truth for who the user
  // wants authorized — msgvault only knows who already *is* authorized.
  const loadDesired = useCallback(async () => {
    try {
      const s = await fetchMsgvaultStatus();
      setDesired((s.desired_emails || []).map((e) => e.toLowerCase()));
    } catch {
      /* leave desired as-is */
    }
  }, []);

  useEffect(() => {
    loadAccounts().then((emails) => runEstimate(emails));
    loadDesired();
  }, [loadAccounts, loadDesired, runEstimate]);

  async function addAccount(email: string) {
    setLinking(true);
    setLinkingEmail(email.toLowerCase());
    setError(null);
    try {
      // Add = register + add as OAuth test user, left PENDING (skipAuthorize).
      // The user authorizes it separately via its Authorize button. Background
      // job; wait for it to finish before reloading.
      const { job } = await runSetupAction({ action: "gmail-link-emails", emails: email, skipAuthorize: true });
      let current = job;
      while (current.status === "running" || current.status === "pending") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        current = await fetchSetupJob(job.id);
      }
      setNewEmail("");
      setAddOpen(false);
      if (current.status === "completed") {
        runEstimate(await loadAccounts());
        loadDesired();
      } else {
        setError(current.stderr || "Could not add that account.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add account");
    } finally {
      setLinking(false);
      setLinkingEmail(null);
    }
  }

  // Authorize an account that's already in the desired/test-user list: run the
  // per-account grant (add-account) only — no add-test-users / OAuth console
  // panel, since it was already added at create time.
  async function authorizeOnly(email: string) {
    setLinking(true);
    setLinkingEmail(email.toLowerCase());
    setError(null);
    try {
      const { job } = await runGmailAuthorize({ email });
      let current = job;
      while (current.status === "running" || current.status === "pending") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        current = await fetchSetupJob(job.id);
      }
      if (current.status === "completed") {
        runEstimate(await loadAccounts());
        loadDesired();
      } else {
        setError(current.stderr || `Couldn't authorize ${email}.`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authorization failed");
    } finally {
      setLinking(false);
      setLinkingEmail(null);
    }
  }

  async function handleSync() {
    setSyncing(true);
    setSyncDone(null);
    setError(null);
    const emails = accounts.map((account) => account.email);
    try {
      const { job } = await runGmailWindowSync({ window: selected, accounts: emails });
      let current = job;
      while (current.status === "running" || current.status === "pending") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        current = await fetchSetupJob(job.id);
      }
      if (current.status === "completed") {
        setSyncDone(`Synced ${selectedLabel}. Refreshing…`);
        runEstimate(await loadAccounts());
        onChange?.();
        setSyncDone(`Synced ${selectedLabel}`);
      } else {
        setError(current.stderr || "Sync did not complete.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  }

  const current = totals[selected];
  const selectedLabel = SYNC_WINDOWS.find((w) => w.id === selected)?.label.toLowerCase() ?? "";
  const accountCount = accounts.length;
  const hasAccounts = accountCount > 0;
  // Desired emails the user picked at create time that aren't authorized yet —
  // shown with an Authorize button so they don't have to retype them.
  const authorizedSet = new Set(accounts.map((a) => (a.email || "").toLowerCase()));
  const pending = desired.filter((email) => !authorizedSet.has(email));

  return (
    <div className="space-y-3">
      {/* Accounts */}
      <div className="rounded-lg border bg-muted/30 p-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">Gmail accounts</span>
          <button
            type="button"
            onClick={() => setAddOpen((open) => !open)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5" /> Add
          </button>
        </div>

        {accountsLoading ? (
          <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading accounts…
          </div>
        ) : hasAccounts || pending.length > 0 ? (
          <ul className="mt-2 space-y-1">
            {accounts.map((account) => (
              <li key={account.email} className="flex items-center gap-2 text-sm">
                <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                <span className="break-all">{account.email}</span>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {account.message_count > 0
                    ? `${account.message_count.toLocaleString()} synced`
                    : "Not synced yet"}
                </span>
              </li>
            ))}
            {pending.map((email) => (
              <li key={email} className="flex items-center gap-2 text-sm">
                <Clock className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="break-all text-muted-foreground">{email}</span>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto h-7"
                  disabled={linking}
                  onClick={() => authorizeOnly(email)}
                >
                  {linkingEmail === email ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                  {linkingEmail === email ? "Authorizing…" : "Authorize"}
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">No Gmail accounts yet — add one to estimate.</p>
        )}

        {addOpen && (
          <div className="mt-2 space-y-1">
            <div className="flex items-end gap-2">
              <Input
                value={newEmail}
                onChange={(event) => setNewEmail(event.target.value)}
                placeholder="name@gmail.com"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && newEmail.trim()) {
                    event.preventDefault();
                    addAccount(newEmail.trim());
                  }
                }}
              />
              <Button size="sm" disabled={!newEmail.trim() || linking} onClick={() => addAccount(newEmail.trim())}>
                {linking ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null} Add
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">Adds the account as a test user, then it appears below to Authorize.</p>
          </div>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        We sync the people you actually email — newsletters, promotions and social are skipped. Pick how far back to go.
      </p>

      <div className="grid grid-cols-4 gap-2">
        {SYNC_WINDOWS.map((window) => {
          const total = totals[window.id];
          const isSelected = selected === window.id;
          const pending = hasAccounts && !total && (estimating || (window.id === "all" && allPending));
          return (
            <button
              key={window.id}
              type="button"
              disabled={!hasAccounts}
              onClick={() => setSelected(window.id)}
              className={cn(
                "flex flex-col items-center gap-0.5 rounded-lg border px-2 py-3 text-center transition-colors disabled:opacity-50",
                isSelected ? "border-primary bg-primary/5" : "border-muted-foreground/20 hover:border-muted-foreground/40"
              )}
            >
              <span className={cn("text-sm font-medium", isSelected ? "text-primary" : "")}>{window.label}</span>
              <span className="flex h-4 items-center justify-center text-xs text-muted-foreground">
                {pending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : total ? (
                  `${total.messages.toLocaleString()}${total.truncated ? "+" : ""}`
                ) : (
                  "—"
                )}
              </span>
            </button>
          );
        })}
      </div>

      {current && (
        <div className="flex items-center justify-center gap-5 rounded-md bg-muted/50 px-4 py-2.5 text-sm">
          <span className="flex items-center gap-1.5">
            <MessageSquare className="h-4 w-4 text-muted-foreground" />
            <span className="font-medium">
              {current.messages.toLocaleString()}
              {current.truncated ? "+" : ""}
            </span>
            <span className="text-muted-foreground">
              emails{accountCount > 1 ? ` across ${accountCount} accounts` : ""}
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <Clock className="h-4 w-4 text-muted-foreground" />
            <span className="text-muted-foreground">about</span>
            <span className="font-medium">{current.est_minutes} min</span>
            <span className="text-muted-foreground">to sync</span>
          </span>
        </div>
      )}

      <Button className="w-full" disabled={!hasAccounts || syncing} onClick={handleSync}>
        {syncing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Mail className="mr-2 h-4 w-4" />}
        {syncing ? `Syncing ${selectedLabel}…` : `Sync ${selectedLabel}`}
      </Button>
      {syncDone && (
        <p className="flex items-center justify-center gap-1.5 text-center text-sm text-emerald-600">
          {syncDone.endsWith("…") ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <CheckCircle2 className="h-4 w-4" />
          )}
          {syncDone}
        </p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
