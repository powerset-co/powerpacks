import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Download,
  GitBranch,
  Loader2,
  Power,
  RefreshCcw,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchSetupJob,
  fetchSystemDaemonStatus,
  fetchSystemHealth,
  fetchSystemUpdateStatus,
  restartSystem,
  startSystemUpdate,
  type SystemDaemonStatus,
  type SystemUpdateStatus,
} from "./powerpacksApi";

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
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [rebooting, setRebooting] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [next, daemonNext] = await Promise.all([
        fetchSystemUpdateStatus(),
        fetchSystemDaemonStatus().catch(() => null),
      ]);
      setStatus(next);
      setDaemon(daemonNext);
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
          <p className="mt-1 text-sm text-muted-foreground">Update Powerpacks and manage the console process.</p>
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

      {/* Update */}
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

      {/* Console process / daemon */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Cpu className="h-5 w-5" /> Console process</CardTitle>
          <CardDescription>
            Run the console as a background daemon and restart it. Always does the same thing — safe to click repeatedly.
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
