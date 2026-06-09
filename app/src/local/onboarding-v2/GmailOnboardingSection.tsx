import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, DollarSign, KeyRound, Loader2, Mail } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  checkGmailTokens,
  dryRunOnboardingV2Gmail,
  fetchOnboardingV2GmailStatus,
  runOnboardingV2Gmail,
  runSetupAction,
} from "../powerpacksApi";
import type { SetupJob } from "../types";
import { OnboardingStatusCard } from "./OnboardingStatusCard";
import {
  arrayValue,
  commandText,
  GMAIL_DEFAULT_STAGES,
  numberValue,
  objectValue,
  stringValue,
  type JsonObject,
} from "./utils";

export function GmailOnboardingSection() {
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [dryRun, setDryRun] = useState<JsonObject | null>(null);
  const [latestJob, setLatestJob] = useState<SetupJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [linking, setLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [newEmail, setNewEmail] = useState("");
  const [maxEnrich, setMaxEnrich] = useState(0);
  const [reauthEmail, setReauthEmail] = useState<string | null>(null);
  const [reauthing, setReauthing] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchOnboardingV2GmailStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load Gmail onboarding status");
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const dryRunOutput = objectValue(dryRun?.output);
  const linkedAccountList = (Array.isArray(status?.linked_accounts) ? status?.linked_accounts : dryRunOutput.linked_accounts) as unknown[] | undefined;
  const accountEmails = (linkedAccountList || []).map((item) => stringValue(item)).filter(Boolean);
  // Expired accounts are surfaced as structured data from the inspect stage.
  // Once visible, we poll the token check endpoint every 5s so re-authorized
  // accounts flip to "connected" automatically.
  const statusExpired = (Array.isArray(status?.expired_accounts) ? status.expired_accounts : [])
    .map((v) => stringValue(v)).filter(Boolean);
  const [liveExpired, setLiveExpired] = useState<string[] | null>(null);
  const expiredEmails = liveExpired ?? statusExpired;

  useEffect(() => {
    if (statusExpired.length === 0) { setLiveExpired(null); return; }
    let cancelled = false;
    const poll = async () => {
      try {
        const { expired } = await checkGmailTokens(statusExpired);
        if (!cancelled) setLiveExpired(expired);
      } catch { /* ignore */ }
    };
    poll();
    const timer = window.setInterval(poll, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [statusExpired.join(",")]);  // eslint-disable-line react-hooks/exhaustive-deps

  const discoveredAccounts = arrayValue(status?.discovered_accounts);
  const unlinkedAccounts = discoveredAccounts.filter((row) => !accountEmails.includes(stringValue(row.account_email)));
  const latestCommand = commandText(latestJob?.command || dryRun?.command);
  const latestOutput = latestJob?.output || dryRun?.output || null;
  const latestStdout = stringValue(latestJob?.stdout || dryRun?.stdout);
  const latestStderr = stringValue(latestJob?.stderr || dryRun?.stderr);
  const parallelEstimate = objectValue(dryRunOutput.parallel_spend_estimate);
  const hasParallelEstimate = Object.keys(parallelEstimate).length > 0;
  const parallelPendingContacts = numberValue(parallelEstimate.pending_contacts);
  const parallelEstimatedUsd = numberValue(parallelEstimate.estimated_usd);

  // Blocked approval: enrich stage is blocked_approval with a spend estimate
  const enrichStage = objectValue(objectValue(status?.stages).enrich);
  const isBlocked = stringValue(status?.status) === "blocked_approval"
    || stringValue(enrichStage.status) === "blocked_approval";
  const blockedSpend = objectValue(
    objectValue(enrichStage.payload).parallel_spend_estimate
    || status?.parallel_spend_estimate
  );
  const blockedPending = numberValue(blockedSpend.pending_contacts);
  const blockedCost = numberValue(blockedSpend.estimated_usd);

  async function handleDryRun() {
    setLoading(true);
    setError(null);
    try {
      const response = await dryRunOnboardingV2Gmail();
      setDryRun(response);
      setLatestJob(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dry-run failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleRun() {
    setLoading(true);
    setError(null);
    try {
      const response = await runOnboardingV2Gmail({
        ...(maxEnrich > 0 ? { maxEnrich } : {}),
      });
      setLatestJob(response.job);
      setStatus(response.status);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  /** Link accounts already present in msgvault — just records them in accounts.json. */
  async function handleLinkDiscoveredAccounts(emails: string[]) {
    setLinking(true);
    setError(null);
    const minSpinner = new Promise((r) => setTimeout(r, 1000));
    try {
      // These accounts are already authorized in msgvault, so we only need to
      // record them via the lightweight --gmail-account path (no browser OAuth).
      for (const email of emails) {
        await runSetupAction({ action: "gmail-account", email });
      }
      await minSpinner;
      setSelectedAccounts(new Set());
      await loadStatus();
    } catch (err) {
      await minSpinner;
      setError(err instanceof Error ? err.message : "Failed to link accounts");
    } finally {
      setLinking(false);
    }
  }

  /** Add a new email — runs full OAuth browser-setup + authorization flow. */
  async function handleAddNewEmail(email: string) {
    setLinking(true);
    setError(null);
    try {
      await runSetupAction({ action: "gmail-link-emails", emails: email });
      setNewEmail("");
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add account");
    } finally {
      setLinking(false);
    }
  }

  return (
    <div className="space-y-4">
      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
        </Card>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Mail className="h-4 w-4" /> Gmail
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Runs the linked Gmail accounts through sync, discovery, enrichment, and indexing in one shot. Parallel.ai enrichment spend is auto-approved so the single button completes end-to-end.
          </p>

          {/* Linked accounts */}
          <div className="rounded-lg border bg-muted/30 p-3 text-sm">
            <div className="text-muted-foreground">Linked Gmail accounts</div>
            {accountEmails.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {accountEmails.map((email) => (
                  <li key={email} className="flex items-center gap-2 break-all font-medium">
                    <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" /> {email}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="mt-1 font-medium">No Gmail accounts linked yet. Connect below, then run.</div>
            )}
          </div>

          {/* Connect flow: discovered msgvault accounts */}
          {unlinkedAccounts.length > 0 && (
            <div className="rounded-lg border border-primary/20 bg-primary/5 p-3 text-sm">
              <div className="font-medium">Available Gmail accounts in msgvault</div>
              <div className="mt-2 space-y-2">
                {unlinkedAccounts.map((row) => {
                  const email = stringValue(row.account_email);
                  const count = numberValue(row.message_count);
                  const checked = selectedAccounts.has(email);
                  return (
                    <label key={email} className="flex cursor-pointer items-center gap-2">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => {
                          setSelectedAccounts((prev) => {
                            const next = new Set(prev);
                            if (next.has(email)) next.delete(email);
                            else next.add(email);
                            return next;
                          });
                        }}
                        className="h-4 w-4 rounded border-gray-300"
                      />
                      <span className="break-all font-medium">{email}</span>
                      {count > 0 && <span className="text-muted-foreground">({count.toLocaleString()} messages)</span>}
                    </label>
                  );
                })}
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  disabled={selectedAccounts.size === 0 || linking}
                  onClick={() => handleLinkDiscoveredAccounts(Array.from(selectedAccounts))}
                >
                  {linking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  Link {selectedAccounts.size > 0 ? selectedAccounts.size : ""} account{selectedAccounts.size !== 1 ? "s" : ""}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={linking}
                  onClick={() => {
                    setSelectedAccounts(new Set(unlinkedAccounts.map((row) => stringValue(row.account_email))));
                  }}
                >
                  Select all
                </Button>
              </div>
            </div>
          )}

          {/* Re-authorize expired tokens */}
          {statusExpired.length > 0 && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
              <div className="font-medium text-destructive">Expired Gmail tokens</div>
              <p className="mt-1 text-muted-foreground">
                Re-authorize each account below. The status updates automatically.
              </p>
              <div className="mt-2 space-y-2">
                {statusExpired.map((email) => {
                  const isFixed = !expiredEmails.includes(email);
                  const isThisReauthing = reauthing && reauthEmail === email;
                  return (
                    <div key={email} className="flex items-center gap-2">
                      {isFixed ? (
                        <>
                          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                          <span className="break-all font-medium text-emerald-700">{email}</span>
                          <span className="text-xs text-emerald-600">Connected</span>
                        </>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={reauthing}
                          onClick={async () => {
                            setReauthEmail(email);
                            setReauthing(true);
                            setError(null);
                            try {
                              await runSetupAction({ action: "gmail-reauth", email });
                            } catch (err) {
                              setError(err instanceof Error ? err.message : "Re-authorization failed");
                            } finally {
                              setReauthing(false);
                              setReauthEmail(null);
                            }
                          }}
                        >
                          {isThisReauthing
                            ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            : <KeyRound className="mr-2 h-4 w-4" />}
                          Re-authorize {email}
                        </Button>
                      )}
                    </div>
                  );
                })}
              </div>
              {expiredEmails.length === 0 && (
                <p className="mt-3 text-sm font-medium text-emerald-700">
                  All accounts re-authorized! Run Gmail v2 again.
                </p>
              )}
            </div>
          )}

          {/* Add a new email not in msgvault */}
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-0 flex-1 space-y-1 text-sm">
              <span className="font-medium">Add another Gmail address</span>
              <Input
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                placeholder="name@gmail.com"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newEmail.trim()) {
                    e.preventDefault();
                    handleAddNewEmail(newEmail.trim());
                  }
                }}
              />
            </label>
            <Button
              size="sm"
              variant="outline"
              disabled={!newEmail.trim() || linking}
              onClick={() => handleAddNewEmail(newEmail.trim())}
            >
              {linking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
              Add
            </Button>
          </div>

          {/* Parallel.ai spend estimate */}
          {hasParallelEstimate && (
            <div className="rounded-lg border bg-muted/30 p-3 text-sm">
              <div className="text-muted-foreground">Estimated Parallel.ai enrichment spend</div>
              <div className="mt-1 font-medium">
                ${parallelEstimatedUsd.toFixed(2)}{" "}
                <span className="text-muted-foreground">
                  ({parallelPendingContacts} contact{parallelPendingContacts === 1 ? "" : "s"} to resolve, auto-approved)
                </span>
              </div>
              {parallelPendingContacts === 0 && (
                <div className="mt-1 text-xs text-muted-foreground">
                  Run a dry-run after discovery to see the lookup count; the queue is built during the discover stage.
                </div>
              )}
            </div>
          )}

          {/* Approval gate */}
          {isBlocked && blockedPending > 0 && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
              <div className="font-medium text-amber-900">Approval needed</div>
              <p className="mt-1 text-amber-800">
                {blockedPending} contact{blockedPending !== 1 ? "s" : ""} need Parallel.ai resolution.
                Estimated cost: <span className="font-semibold">${blockedCost.toFixed(2)}</span>
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button
                  size="sm"
                  disabled={loading}
                  onClick={async () => {
                    setLoading(true);
                    setError(null);
                    try {
                      const response = await runOnboardingV2Gmail({
                        approveSpend: true,
                        continueRun: true,
                        ...(maxEnrich > 0 ? { maxEnrich } : {}),
                      });
                      setLatestJob(response.job);
                      setStatus(response.status);
                      await loadStatus();
                    } catch (err) {
                      setError(err instanceof Error ? err.message : "Run failed");
                    } finally {
                      setLoading(false);
                    }
                  }}
                >
                  {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <DollarSign className="mr-2 h-4 w-4" />}
                  Approve ${blockedCost.toFixed(2)} and continue
                </Button>
              </div>
            </div>
          )}

          {/* Action buttons */}
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={loading || accountEmails.length === 0} onClick={handleRun}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null} Run Gmail v2
            </Button>
          </div>

          {/* Latest command output */}
          {(latestCommand || latestOutput || latestStdout || latestStderr) && (
            <div className="rounded-lg border bg-muted/30 p-3 text-sm">
              <div className="mb-2 font-medium">Command</div>
              <pre className="overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{latestCommand || "—"}</pre>
              {latestOutput ? (
                <>
                  <div className="mb-2 mt-3 font-medium">Output</div>
                  <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{JSON.stringify(latestOutput, null, 2)}</pre>
                </>
              ) : null}
              {latestStdout ? (
                <>
                  <div className="mb-2 mt-3 font-medium">Stdout</div>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{latestStdout}</pre>
                </>
              ) : null}
              {latestStderr ? (
                <>
                  <div className="mb-2 mt-3 font-medium">Stderr</div>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap text-xs text-destructive">{latestStderr}</pre>
                </>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>
      <OnboardingStatusCard status={status} defaultStages={GMAIL_DEFAULT_STAGES} />
    </div>
  );
}
