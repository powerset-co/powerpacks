import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchOnboardingV3LinkedInStatus, runOnboardingV3LinkedIn, uploadLinkedInCsv } from "./powerpacksApi";
import { OnboardingStatusCard } from "./onboarding-v2/OnboardingStatusCard";
import type { JsonObject } from "./onboarding-v2/utils";

const V3_STAGES = [
  { id: "importing", label: "Importing contacts" },
  { id: "indexing", label: "Building search index" },
];

function countConnections(content: string): number {
  const lines = content.split(/\r?\n/);
  const headerIndex = lines.findIndex((line) => line.startsWith("First Name,"));
  if (headerIndex < 0) return 0;
  return lines.slice(headerIndex + 1).filter((line) => line.trim().length > 0).length;
}

// Calibrated from live runs: ~100s fixed (dispatch + duckdb + download),
// RapidAPI ~200/min worst case, indexing ~0.15s/person. Shared-cache hits
// only make this faster.
function estimateMinutes(connections: number): number {
  const seconds = 100 + connections / 3.3 + 0.15 * connections;
  return Math.max(1, Math.round(seconds / 60));
}

export function LocalOnboardingV3Page() {
  const [fileName, setFileName] = useState("");
  const [csvPath, setCsvPath] = useState("");
  const [connections, setConnections] = useState(0);
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchOnboardingV3LinkedInStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load status");
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const running = String(status?.status || "") === "running" || Boolean(status?.active_job);
  const eta = useMemo(() => (connections > 0 ? estimateMinutes(connections) : 0), [connections]);

  async function handleFile(file?: File | null) {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const content = await file.text();
      setConnections(countConnections(content));
      const uploaded = await uploadLinkedInCsv(file);
      setCsvPath(uploaded.path);
      setFileName(file.name);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleProcess() {
    if (!csvPath) return;
    setStarting(true);
    setError(null);
    try {
      const result = await runOnboardingV3LinkedIn({ csvPath });
      setStatus((result.status as JsonObject) || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start");
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div>
        <h2 className="text-2xl font-semibold">Import your LinkedIn network</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Drop in your LinkedIn <span className="font-mono">Connections.csv</span> export and get a searchable
          local index. Processing runs in the team cloud; nothing else to set up.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Connections.csv</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 p-8 text-center transition-colors hover:border-muted-foreground/50">
            <Upload className="h-6 w-6 text-muted-foreground" />
            <span className="text-sm font-medium">
              {fileName || "Click to choose your Connections.csv"}
            </span>
            {fileName && connections > 0 && (
              <span className="text-xs text-muted-foreground">
                {connections} connections · about {eta} min to process
              </span>
            )}
            <input
              type="file"
              accept=".csv"
              className="hidden"
              disabled={uploading || running}
              onChange={(event) => handleFile(event.target.files?.[0])}
            />
          </label>
          <Button className="w-full" disabled={!csvPath || uploading || starting || running} onClick={handleProcess}>
            {(starting || running) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {running ? "Processing…" : "Process"}
          </Button>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>

      <OnboardingStatusCard status={status} defaultStages={V3_STAGES} />
    </div>
  );
}
