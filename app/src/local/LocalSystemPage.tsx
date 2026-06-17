import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Download,
  ExternalLink,
  GitBranch,
  KeyRound,
  Loader2,
  LogIn,
  Power,
  RefreshCcw,
  XCircle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchSetupJob,
  fetchSystemDaemonStatus,
  fetchSystemHealth,
  fetchSystemReadiness,
  fetchSystemUpdateStatus,
  restartSystem,
  startSystemUpdate,
  type SystemDaemonStatus,
  type SystemReadiness,
  type SystemUpdateStatus,
} from "./powerpacksApi";

function labelForReq(readiness: SystemReadiness, req: string): string {
  if (req === "login") return "sign-in";
  return readiness.secrets.find((s) => s.key === req)?.label || req;
}

// After a self-restart the current server dies and a fresh one comes up on the
// same port. Wait out the kill window, then poll health until it answers, then
// reload. This is the FE half of the "yank the batteries and fall on it" reboot.
async function waitForServerAndReload(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 3000));
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    if (await fetchSystemHealth()) {
      window.location.reload();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  window.location.reload();
}

export function LocalSystemPage() {
  const [status, setStatus] = useState<SystemUpdateStatus | null>(null);
  const [daemon, setDaemon] = useState<SystemDaemonStatus | null>(null);
  const [readiness, setReadiness] = useState<SystemReadiness | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [rebooting, setRebooting] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [next, daemonNext, readinessNext] = await Promise.all([
        fetchSystemUpdateStatus(),
        fetchSystemDaemonStatus().catch(() => null),
        fetchSystemReadiness().catch(() => null),
      ]);
      setStatus(next);
      setDaemon(daemonNext);
      setReadiness(readinessNext);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load system status");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleUpdate = useCallback(async () => {
    setUpdating(true);
    setUpdateError(null);
    try {
      const { job, auto_restart } = await startSystemUpdate();
      let current = job;
      while (current.status === "running" || current.status === "pending") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        current = await fetchSetupJob(job.id);
      }
      if (current.status !== "completed") {
        setUpdateError(current.stderr || current.stdout || "Update did not complete.");
        setUpdating(false);
        return;
      }
      await refresh();
      if (auto_restart) {
        setRebooting(true);
        await restartSystem();
        await waitForServerAndReload();
      } else {
        setUpdating(false);
      }
    } catch (err) {
      setUpdateError(err instanceof Error ? err.message : "Update failed");
      setUpdating(false);
    }
  }, [refresh]);

  const handleReboot = useCallback(async () => {
    if (!window.confirm(
      "Reboot the Powerpacks Console?\n\nThis installs the launchd daemon, kills the current server, and relaunches it. The page will reconnect automatically.",
    )) {
      return;
    }
    setRebooting(true);
    setUpdateError(null);
    try {
      await restartSystem();
      await waitForServerAndReload();
    } catch (err) {
      setUpdateError(err instanceof Error ? err.message : "Reboot failed");
      setRebooting(false);
    }
  }, []);

  if (isLoading && !status) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" /> Loading system status
      </div>
    );
  }

  const upToDate = status ? !status.update_available : true;
  const hostList = status
    ? [status.hosts.claude ? "Claude Code" : null, status.hosts.codex ? "Codex" : null].filter(Boolean).join(", ") || "none detected"
    : "—";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">System</h2>
          <p className="mt-1 text-sm text-muted-foreground">Updates and console process control</p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={isLoading || rebooting}>
          <RefreshCcw className="h-4 w-4" /> Refresh
        </Button>
      </div>

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
        </Card>
      )}
      {updateError && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-3 text-sm text-destructive whitespace-pre-wrap">{updateError}</CardContent>
        </Card>
      )}

      {/* Readiness card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {readiness?.ready ? (
              <><CheckCircle2 className="h-5 w-5 text-emerald-600" /> Ready to import &amp; index</>
            ) : (
              <><AlertTriangle className="h-5 w-5 text-amber-500" /> Setup incomplete</>
            )}
          </CardTitle>
          <CardDescription>Secrets &amp; readiness — what each capability needs.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2 text-sm">
            <LogIn className="h-4 w-4 text-muted-foreground" />
            {readiness?.login.logged_in ? (
              <span>Signed in as <span className="font-medium">{readiness.login.email || "Powerset"}</span></span>
            ) : (
              <span className="text-amber-600">
                Not signed in — run <code className="rounded bg-muted px-1">$powerset login</code> then <code className="rounded bg-muted px-1">$powerset env pull</code>
              </span>
            )}
          </div>

          <div className="space-y-2">
            {readiness?.capabilities.map((cap) => {
              const Icon = cap.satisfied ? CheckCircle2 : cap.core ? XCircle : AlertTriangle;
              const color = cap.satisfied ? "text-emerald-600" : cap.core ? "text-destructive" : "text-muted-foreground";
              return (
                <div key={cap.id} className="rounded-md border border-border/60 px-3 py-2">
                  <div className="flex items-center gap-2 text-sm">
                    <Icon className={`h-4 w-4 ${color}`} />
                    <span className="font-medium">{cap.label}</span>
                    {!cap.core && <Badge variant="outline" className="text-[10px]">optional</Badge>}
                  </div>
                  {cap.description && <p className="mt-0.5 pl-6 text-xs text-muted-foreground">{cap.description}</p>}
                  {!cap.satisfied && cap.missing.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1 pl-6">
                      {cap.missing.map((m) => (
                        <Badge key={m} variant="secondary" className="text-[10px]">needs {readiness ? labelForReq(readiness, m) : m}</Badge>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            {readiness?.secrets.map((s) => (
              <div key={s.key} className="flex items-center justify-between gap-2 rounded-md border border-border/60 px-3 py-1.5 text-sm">
                <span className="flex items-center gap-1.5">
                  <KeyRound className="h-3.5 w-3.5 text-muted-foreground" />
                  {s.label}
                  {s.optional && <Badge variant="outline" className="text-[10px]">optional</Badge>}
                </span>
                {s.satisfied ? (
                  <Badge variant="secondary" className="gap-1 text-[10px]"><CheckCircle2 className="h-3 w-3 text-emerald-600" /> set</Badge>
                ) : s.writable ? (
                  <a href="/env" className="text-xs font-medium text-primary hover:underline">Add key</a>
                ) : s.fix === "login_pull" ? (
                  <span className="text-[10px] text-muted-foreground">sign in + env pull</span>
                ) : s.getUrl ? (
                  <a href={s.getUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-xs text-primary hover:underline">get <ExternalLink className="h-3 w-3" /></a>
                ) : (
                  <Badge variant="outline" className="text-[10px]">missing</Badge>
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Update card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {upToDate ? (
              <><CheckCircle2 className="h-5 w-5 text-emerald-600" /> Up to date</>
            ) : (
              <><AlertTriangle className="h-5 w-5 text-amber-500" /> Update available</>
            )}
          </CardTitle>
          <CardDescription>
            Pulls the latest from GitHub and reinstalls the detected agent host(s).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <p className="text-xs text-muted-foreground">powerpacks</p>
              <p className="text-base font-medium">{status?.versions.powerpacks || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">console</p>
              <p className="text-base font-medium">{status?.versions.console || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Branch</p>
              <p className="flex items-center gap-1 text-base font-medium"><GitBranch className="h-3.5 w-3.5" />{status?.branch || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Commits behind</p>
              <p className="text-base font-medium">{status?.behind ?? 0}</p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>local <code className="rounded bg-muted px-1 py-0.5">{status?.short_current || "—"}</code></span>
            <span>→ remote <code className="rounded bg-muted px-1 py-0.5">{status?.short_latest || "—"}</code></span>
            <span>· updates: {hostList}</span>
          </div>

          {status?.dirty && (
            <div className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-sm text-amber-600">
              <AlertTriangle className="h-4 w-4" /> Working tree has local changes — commit or stash before updating.
            </div>
          )}

          {!upToDate && (
            <Button
              onClick={handleUpdate}
              disabled={updating || rebooting || Boolean(status?.dirty)}
              className="w-full sm:w-auto"
            >
              {updating ? (
                <><Loader2 className="h-4 w-4 animate-spin" /> Updating…</>
              ) : (
                <><Download className="h-4 w-4" /> Update &amp; restart</>
              )}
            </Button>
          )}
        </CardContent>
      </Card>

      {/* Reboot / daemon card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Cpu className="h-5 w-5" /> Console process</CardTitle>
          <CardDescription>
            Install the background daemon and restart the console. Always does the same thing — safe to click repeatedly to verify self-recovery.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant={daemon?.daemonized ? "secondary" : "outline"} className="gap-1">
              {daemon?.daemonized ? "daemonized" : "not daemonized"}
            </Badge>
            <Badge variant={daemon?.running ? "secondary" : "outline"} className="gap-1">
              {daemon?.running ? `listening on :${daemon?.port}` : "not listening"}
            </Badge>
            {daemon?.pid ? <span className="text-xs text-muted-foreground">pid {daemon.pid}</span> : null}
          </div>

          <Button onClick={handleReboot} variant="destructive" disabled={rebooting}>
            {rebooting ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Rebooting & reconnecting…</>
            ) : (
              <><Power className="h-4 w-4" /> Install daemon &amp; reboot</>
            )}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
