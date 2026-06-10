import { useCallback, useEffect, useState } from "react";
import { Loader2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  dryRunOnboardingV2LinkedIn,
  fetchOnboardingV2LinkedInStatus,
  runOnboardingV2LinkedIn,
  uploadLinkedInCsv,
} from "../powerpacksApi";
import type { SetupJob } from "../types";
import { OnboardingStatusCard } from "./OnboardingStatusCard";
import {
  commandText,
  DEFAULT_LINKEDIN_CSV,
  LINKEDIN_DEFAULT_STAGES,
  objectValue,
  selectedFileDisplayPath,
  stringValue,
  type JsonObject,
} from "./utils";

export function LinkedInOnboardingSection() {
  const [csvPath, setCsvPath] = useState(DEFAULT_LINKEDIN_CSV);
  const [displayCsvPath, setDisplayCsvPath] = useState(DEFAULT_LINKEDIN_CSV);
  const [uploadedDisplayPath, setUploadedDisplayPath] = useState("");
  const [uploadedCachePath, setUploadedCachePath] = useState("");
  const sourceLabel = "local";
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

  const dryRunOutput = objectValue(null);
  const csvStats = objectValue(dryRunOutput.csv_stats || status?.csv_stats);
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
      const displayPath = selectedFileDisplayPath(file);
      setCsvPath(uploaded.path);
      setDisplayCsvPath(displayPath);
      setUploadedDisplayPath(displayPath);
      setUploadedCachePath(uploaded.path);
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
          <div>
            <label className="space-y-1 text-sm">
              <span className="font-medium">CSV path</span>
              <Input
                value={displayCsvPath}
                readOnly={Boolean(uploadedCachePath)}
                onChange={(event) => {
                  if (uploadedCachePath) return;
                  setDisplayCsvPath(event.target.value);
                  setCsvPath(event.target.value);
                  setUploadedDisplayPath("");
                  setUploadedCachePath("");
                }}
              />
              {uploadedCachePath ? (
                <span className="block text-xs text-muted-foreground">
                  Selected upload: {uploadedDisplayPath}. Powerpacks will run from the cached upload copy; upload another CSV to change it.
                </span>
              ) : (
                <span className="block text-xs text-muted-foreground">
                  Use the restored local Connections.csv, or upload a CSV from your machine.
                </span>
              )}
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

      <OnboardingStatusCard status={status} defaultStages={LINKEDIN_DEFAULT_STAGES} />
    </div>
  );
}
