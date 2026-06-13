import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  ExternalLink,
  FileCheck2,
  KeyRound,
  Loader2,
  LogIn,
  Search,
  Terminal,
  Upload,
  Users,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  fetchEnvStatus,
  fetchOnboardingV3LinkedInStatus,
  fetchPowersetWhoami,
  fetchSetupJob,
  runOnboardingV3LinkedIn,
  runPowersetLogin,
  updateEnvKeys,
  uploadLinkedInCsv,
  type PowersetWhoami,
} from "./powerpacksApi";
import { OnboardingStatusCard } from "./onboarding-v2/OnboardingStatusCard";
import type { EnvKeyStatus, EnvStatusResponse, JsonObject } from "./types";

const V3_STAGES = [
  { id: "importing", label: "Importing contacts" },
  { id: "indexing", label: "Building search index" },
];

const BYO_KEYS = ["OPENAI_API_KEY", "RAPIDAPI_LINKEDIN_KEY", "PARALLEL_API_KEY"];

const WIZARD_STEPS = [
  { id: "connect", label: "Connect" },
  { id: "import", label: "Import LinkedIn" },
  { id: "search", label: "First search" },
] as const;
type StepId = (typeof WIZARD_STEPS)[number]["id"];

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

function Stepper({ steps, active, done, onSelect }: {
  steps: ReadonlyArray<{ id: StepId; label: string }>;
  active: StepId;
  done: Record<StepId, boolean>;
  onSelect: (id: StepId) => void;
}) {
  return (
    <div className="flex items-center">
      {steps.map((step, index) => {
        const complete = done[step.id];
        const isActive = active === step.id;
        return (
          <div key={step.id} className="flex flex-1 items-center last:flex-none">
            <button
              type="button"
              onClick={() => onSelect(step.id)}
              className="flex items-center gap-2 text-left"
            >
              <span
                className={cn(
                  "flex h-8 w-8 items-center justify-center rounded-full border text-sm font-medium transition-colors",
                  complete
                    ? "border-primary bg-primary text-primary-foreground"
                    : isActive
                      ? "border-primary text-primary"
                      : "border-muted-foreground/30 text-muted-foreground"
                )}
              >
                {complete ? <CheckCircle2 className="h-4 w-4" /> : index + 1}
              </span>
              <span className={cn("text-sm font-medium", isActive ? "" : "text-muted-foreground")}>{step.label}</span>
            </button>
            {index < steps.length - 1 && (
              <span className={cn("mx-3 h-px flex-1", complete ? "bg-primary" : "bg-border")} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function PowersetAuthPanel({ onConnected }: { onConnected: () => void }) {
  const [who, setWho] = useState<PowersetWhoami | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchPowersetWhoami();
      setWho(result);
      if (result.status === "logged_in" && !result.expired) onConnected();
    } catch {
      /* whoami is best-effort */
    }
  }, [onConnected]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function login() {
    setBusy(true);
    setError(null);
    try {
      const { job } = await runPowersetLogin();
      let current = job;
      while (current.status === "running") {
        await new Promise((r) => setTimeout(r, 1500));
        current = await fetchSetupJob(job.id);
      }
      if (current.status !== "completed") {
        setError("Login did not complete. Try again, or use your own keys below.");
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  const loggedIn = who?.status === "logged_in" && !who?.expired;

  return (
    <div className="space-y-3">
      {loggedIn ? (
        <div className="flex items-center gap-3 rounded-lg border border-primary/40 bg-primary/5 p-4">
          <CheckCircle2 className="h-5 w-5 text-primary" />
          <div>
            <div className="text-sm font-medium">Connected to Powerset</div>
            <div className="text-xs text-muted-foreground">{who?.email}</div>
          </div>
        </div>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            Log in once and your provider keys are pulled for you — nothing else to paste.
          </p>
          <Button onClick={login} disabled={busy} className="w-full">
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <LogIn className="mr-2 h-4 w-4" />}
            {busy ? "Complete login in your browser…" : "Login to Powerset"}
          </Button>
        </>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}

function ByoKeysPanel({ onReady }: { onReady: (ready: boolean) => void }) {
  const [status, setStatus] = useState<EnvStatusResponse | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const keys = useMemo<EnvKeyStatus[]>(
    () => (status?.keys || []).filter((k) => BYO_KEYS.includes(k.key)),
    [status]
  );

  const refresh = useCallback(async () => {
    try {
      const result = await fetchEnvStatus();
      setStatus(result);
      onReady(BYO_KEYS.every((key) => result.keys.find((k) => k.key === key)?.satisfied));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load env status");
    }
  }, [onReady]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      await updateEnvKeys(values);
      setValues({});
      setSaved(true);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const dirty = Object.values(values).some((v) => v.trim().length > 0);

  return (
    <div className="space-y-3">
      {keys.map((item) => (
        <div key={item.key} className="space-y-1.5">
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-2 text-sm font-medium">
              {item.satisfied ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              ) : (
                <KeyRound className="h-4 w-4 text-muted-foreground" />
              )}
              {item.provider}
            </label>
            {!item.satisfied && item.getUrl && (
              <a
                href={item.getUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              >
                Get key <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
          <Input
            type="password"
            placeholder={item.satisfied ? `Set (${item.valuePreview || "•••"}) — paste to replace` : `Paste your ${item.provider} key`}
            value={values[item.key] ?? ""}
            onChange={(e) => setValues((prev) => ({ ...prev, [item.key]: e.target.value }))}
          />
        </div>
      ))}
      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={saving || !dirty} variant="secondary">
          {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Save keys
        </Button>
        {saved && <span className="text-sm text-emerald-600">Saved to .env</span>}
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}

function ImportPanel({ onDone }: { onDone: () => void }) {
  const [fileName, setFileName] = useState("");
  const [csvPath, setCsvPath] = useState("");
  const [connections, setConnections] = useState(0);
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const next = await fetchOnboardingV3LinkedInStatus();
      setStatus(next);
      if (String(next?.status || "") === "completed") onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load status");
    }
  }, [onDone]);

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
    <div className="space-y-3">
      <label
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 p-8 text-center transition-colors",
          fileName
            ? "border-solid border-primary/40 bg-primary/5"
            : "border-dashed border-muted-foreground/25 hover:border-muted-foreground/50"
        )}
      >
        {uploading ? (
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        ) : fileName ? (
          <FileCheck2 className="h-6 w-6 text-primary" />
        ) : (
          <Upload className="h-6 w-6 text-muted-foreground" />
        )}
        <span className={cn("text-sm font-medium", fileName ? "text-primary" : "")}>
          {uploading ? "Reading file…" : fileName || "Click to choose your Connections.csv"}
        </span>
        <span className="text-xs text-muted-foreground">
          {fileName ? "Click to choose a different file" : "LinkedIn → Settings → Data privacy → Get a copy of your data → Connections"}
        </span>
        <input
          type="file"
          accept=".csv"
          className="hidden"
          disabled={uploading || running}
          onChange={(event) => handleFile(event.target.files?.[0])}
        />
      </label>

      {fileName && connections > 0 && (
        <div className="flex items-center justify-center gap-5 rounded-md bg-muted/50 px-4 py-2.5 text-sm">
          <span className="flex items-center gap-1.5">
            <Users className="h-4 w-4 text-muted-foreground" />
            <span className="font-medium">{connections.toLocaleString()}</span>
            <span className="text-muted-foreground">connections</span>
          </span>
          <span className="flex items-center gap-1.5">
            <Clock className="h-4 w-4 text-muted-foreground" />
            <span className="text-muted-foreground">about</span>
            <span className="font-medium">{eta} min</span>
            <span className="text-muted-foreground">to process</span>
          </span>
        </div>
      )}

      <Button className="w-full" disabled={!csvPath || uploading || starting || running} onClick={handleProcess}>
        {(starting || running) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {running ? "Processing…" : "Process"}
      </Button>
      {error && <p className="text-sm text-destructive">{error}</p>}

      <OnboardingStatusCard status={status} defaultStages={V3_STAGES} />
    </div>
  );
}

const SEARCH_EXAMPLE = '$search-network "software engineers in SF who worked at early-stage startups"';

function FirstSearchPanel({ repoRoot }: { repoRoot: string }) {
  function openCodex() {
    // Codex.app handles codex://threads/new with prompt= and path= params, so
    // this opens a new Codex chat at the repo with the search prefilled. If the
    // app isn't installed the browser no-ops.
    const params = `prompt=${encodeURIComponent(SEARCH_EXAMPLE)}${
      repoRoot ? `&path=${encodeURIComponent(repoRoot)}` : ""
    }`;
    window.location.href = `codex://threads/new?${params}`;
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Your index is local. Open it in Codex with a network search prefilled — just hit enter.
      </p>

      <div className="rounded-lg border bg-muted/40 p-3">
        <div className="flex items-center justify-between gap-3">
          <code className="truncate font-mono text-sm">{SEARCH_EXAMPLE}</code>
          <Button size="sm" onClick={openCodex}>
            <Terminal className="mr-1 h-4 w-4" /> Codex
          </Button>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        Opens a new Codex chat at this repo. Requires the{" "}
        <a
          href="https://developers.openai.com/codex"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 underline"
        >
          Codex desktop app <ExternalLink className="h-3 w-3" />
        </a>
        .
      </p>
    </div>
  );
}

export function LocalOnboardingV3Page() {
  const [active, setActive] = useState<StepId>("connect");
  const [powersetConnected, setPowersetConnected] = useState(false);
  const [keysReady, setKeysReady] = useState(false);
  const [byoOpen, setByoOpen] = useState(false);
  const [importDone, setImportDone] = useState(false);
  const [repoRoot, setRepoRoot] = useState("");

  useEffect(() => {
    fetchEnvStatus()
      .then((s) => {
        setRepoRoot(s.path.replace(/\/\.env$/, ""));
        setKeysReady(BYO_KEYS.every((key) => s.keys.find((k) => k.key === key)?.satisfied));
      })
      .catch(() => {});
  }, []);

  const done: Record<StepId, boolean> = {
    connect: powersetConnected || keysReady,
    import: importDone,
    search: false,
  };

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div>
        <h2 className="text-2xl font-semibold">Get a searchable network in minutes</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Connect, drop your LinkedIn export, and run your first local search.
        </p>
      </div>

      <Stepper steps={WIZARD_STEPS} active={active} done={done} onSelect={setActive} />

      {active === "connect" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Connect</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <PowersetAuthPanel onConnected={() => setPowersetConnected(true)} />

            <div className="rounded-lg border">
              <button
                type="button"
                onClick={() => setByoOpen((open) => !open)}
                className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm hover:bg-muted/50"
              >
                {byoOpen ? (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                )}
                <span className="font-medium">Bring your own keys</span>
                <span className="text-xs text-muted-foreground">
                  {keysReady ? "all set" : "for open-source / no Powerset account"}
                </span>
                {keysReady && <CheckCircle2 className="ml-auto h-4 w-4 text-emerald-600" />}
              </button>
              {byoOpen && (
                <div className="border-t px-3 py-3">
                  <ByoKeysPanel onReady={setKeysReady} />
                </div>
              )}
            </div>

            <Button className="w-full" disabled={!done.connect} onClick={() => setActive("import")}>
              Continue to import
            </Button>
          </CardContent>
        </Card>
      )}

      {active === "import" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Import your LinkedIn network</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <ImportPanel onDone={() => setImportDone(true)} />
            <Button variant="secondary" className="w-full" onClick={() => setActive("search")}>
              I'm done — try a search
            </Button>
          </CardContent>
        </Card>
      )}

      {active === "search" && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Search className="h-4 w-4" /> Try your first local search
            </CardTitle>
          </CardHeader>
          <CardContent>
            <FirstSearchPanel repoRoot={repoRoot} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
