import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, CircleAlert, CircleDot, Loader2, RefreshCcw, Upload } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  dryRunOnboardingV2LinkedIn,
  fetchOnboardingV2LinkedInStatus,
  runOnboardingV2LinkedIn,
  uploadLinkedInCsv,
} from "./powerpacksApi";
import type { SetupJob } from "./types";

type JsonObject = Record<string, unknown>;

const DEFAULT_LINKEDIN_CSV = ".powerpacks/network-import/discover/linkedin/Connections.csv";
const DEFAULT_STAGES = [
  { id: "inspect", label: "Check LinkedIn CSV" },
  { id: "discover", label: "Import LinkedIn contacts" },
  { id: "enrich", label: "Enrich LinkedIn profiles" },
  { id: "source_people", label: "Save LinkedIn people file" },
  { id: "merge_network", label: "Merge contact sources" },
  { id: "network_duckdb", label: "Prepare contact lookup database" },
  { id: "index_estimate", label: "Estimate search updates" },
  { id: "index_records", label: "Build searchable people records" },
  { id: "search_duckdb", label: "Update local search database" },
];

function selectedFileDisplayPath(file: File): string {
  const fileWithPath = file as File & { path?: string; webkitRelativePath?: string };
  const browserPath = fileWithPath.path || fileWithPath.webkitRelativePath || "";
  if (browserPath && !browserPath.includes("fakepath")) return browserPath;
  return `~/Downloads/${file.name}`;
}

function objectValue(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function arrayValue(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter((item): item is JsonObject => Boolean(item && typeof item === "object" && !Array.isArray(item))) : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function commandText(command: unknown): string {
  if (Array.isArray(command)) return command.map((part) => String(part)).join(" ");
  return stringValue(command);
}

function statusTone(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "completed" || status === "ok" || status === "dry_run") return "default";
  if (status === "failed" || status === "blocked_approval") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

function stageRows(status: JsonObject | null) {
  const order = arrayValue(status?.stage_order);
  const stages = objectValue(status?.stages);
  const activeOrder = order.length > 0 ? order : DEFAULT_STAGES;
  return activeOrder.map((stage, index) => {
    const id = stringValue(stage.id);
    const detail = objectValue(stages[id]);
    return {
      id,
      index: index + 1,
      total: activeOrder.length,
      label: stringValue(stage.label || detail.label || id),
      message: stringValue(detail.message),
      status: stringValue(detail.status || "pending"),
      updatedAt: stringValue(detail.updated_at),
    };
  });
}

export function LocalOnboardingV2Page() {
  const [csvPath, setCsvPath] = useState(DEFAULT_LINKEDIN_CSV);
  const [displayCsvPath, setDisplayCsvPath] = useState(DEFAULT_LINKEDIN_CSV);
  const [sourceLabel, setSourceLabel] = useState("arthur");
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [dryRun, setDryRun] = useState<JsonObject | null>(null);
  const [latestJob, setLatestJob] = useState<SetupJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchOnboardingV2LinkedInStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load onboarding status");
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const statusText = stringValue(status?.status || "missing");
  const progress = Math.max(0, Math.min(1, numberValue(status?.progress)));
  const steps = useMemo(() => stageRows(status), [status]);
  const dryRunOutput = objectValue(dryRun?.output);
  const csvStats = objectValue(dryRunOutput.csv_stats || status?.csv_stats);
  const outputs = objectValue(objectValue(status?.result).outputs || status?.outputs || dryRunOutput.outputs);
  const latestCommand = commandText(latestJob?.command || dryRun?.command);
  const latestOutput = latestJob?.output || dryRun?.output || null;
  const latestStdout = stringValue(latestJob?.stdout || dryRun?.stdout);
  const latestStderr = stringValue(latestJob?.stderr || dryRun?.stderr);

  async function handleUpload(file?: File | null) {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const uploaded = await uploadLinkedInCsv(file);
      setCsvPath(uploaded.path);
      setDisplayCsvPath(selectedFileDisplayPath(file));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleDryRun() {
    setLoading(true);
    setError(null);
    try {
      const response = await dryRunOnboardingV2LinkedIn({ csvPath, sourceLabel });
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
      const response = await runOnboardingV2LinkedIn({ csvPath, sourceLabel });
      setLatestJob(response.job);
      setStatus(response.status);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold">Onboarding v2</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Temporary LinkedIn CSV flow for this PR. It imports the LinkedIn ingestion steps directly, writes people into the local lake, then reuses the existing indexing wrapper.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={loadStatus}>
          <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
        </Button>
      </div>

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">LinkedIn CSV</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 md:grid-cols-[1fr_180px]">
            <label className="space-y-1 text-sm">
              <span className="font-medium">CSV path</span>
              <Input
                value={displayCsvPath}
                onChange={(event) => {
                  setDisplayCsvPath(event.target.value);
                  setCsvPath(event.target.value);
                }}
              />
            </label>
            <label className="space-y-1 text-sm">
              <span className="font-medium">Source label</span>
              <Input value={sourceLabel} onChange={(event) => setSourceLabel(event.target.value)} />
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" disabled={uploading || loading} asChild>
              <label className="cursor-pointer">
                {uploading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
                Upload CSV
                <input className="hidden" type="file" accept=".csv,text/csv" onChange={(event) => handleUpload(event.target.files?.[0])} />
              </label>
            </Button>
            <Button variant="outline" disabled={loading} onClick={handleDryRun}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null} Dry-run
            </Button>
            <Button disabled={loading} onClick={handleRun}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null} Run LinkedIn v2
            </Button>
          </div>
          {Object.keys(csvStats).length > 0 && (
            <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm md:grid-cols-4">
              <div><div className="text-muted-foreground">Valid contacts</div><div className="font-medium">{stringValue(csvStats.valid_contacts || "—")}</div></div>
              <div><div className="text-muted-foreground">Duplicates</div><div className="font-medium">{stringValue(csvStats.duplicates || 0)}</div></div>
              <div><div className="text-muted-foreground">Skipped rows</div><div className="font-medium">{stringValue(csvStats.skipped_invalid || 0)}</div></div>
              <div><div className="text-muted-foreground">Current import</div><div className="font-medium">{String(dryRunOutput.current_import ?? "—")}</div></div>
            </div>
          )}
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

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            Status <Badge variant={statusTone(statusText)}>{statusText.replace(/_/g, " ")}</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          {status?.stale === true && (
            <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
              <CircleAlert className="mt-0.5 h-4 w-4" />
              <span>{stringValue(status.stale_reason) || "This run has not updated recently."}</span>
            </div>
          )}
          <div className="grid gap-2 text-sm md:grid-cols-3">
            <div><div className="text-muted-foreground">Run ID</div><div className="break-all font-medium">{stringValue(status?.run_id || "—")}</div></div>
            <div><div className="text-muted-foreground">Stage</div><div className="font-medium">{stringValue(status?.current_stage || "—")}</div></div>
            <div><div className="text-muted-foreground">Updated</div><div className="font-medium">{stringValue(status?.updated_at || "—")}</div></div>
          </div>
          {steps.length > 0 && (
            <div className="space-y-2">
              <div className="text-sm font-medium">Steps</div>
              {steps.map((step) => {
                const isRunning = step.status === "running";
                const isCompleted = step.status === "completed";
                const isFailed = step.status === "failed" || step.status === "blocked_approval";
                const Icon = isFailed ? CircleAlert : isCompleted ? CheckCircle2 : isRunning ? Loader2 : CircleDot;
                return (
                <div
                  key={step.id}
                  className={cn(
                    "flex items-start gap-2 rounded-md border p-2 text-sm",
                    isRunning && "border-primary/30 bg-primary/5",
                    isCompleted && "border-emerald-500/30 bg-emerald-500/5",
                    isFailed && "border-destructive/40 bg-destructive/5",
                    !isRunning && !isCompleted && !isFailed && "bg-muted/20",
                  )}
                >
                  <Icon className={cn(
                    "mt-0.5 h-4 w-4 shrink-0",
                    isRunning && "animate-spin text-primary",
                    isCompleted && "text-emerald-600",
                    isFailed && "text-destructive",
                    !isRunning && !isCompleted && !isFailed && "text-muted-foreground",
                  )} />
                  <div>
                    <div className="font-medium">{step.label}</div>
                    <div className="text-muted-foreground">{step.message || `${step.index}/${step.total}`}</div>
                  </div>
                </div>
                );
              })}
            </div>
          )}
          {Object.keys(outputs).length > 0 && (
            <div className="rounded-lg border bg-muted/30 p-3 text-sm">
              <div className="mb-2 font-medium">Outputs</div>
              <pre className="overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{JSON.stringify(outputs, null, 2)}</pre>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
