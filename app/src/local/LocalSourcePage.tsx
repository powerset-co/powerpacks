import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, FileCheck2, Loader2, MessageCircle, Sparkles, Upload } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { GmailSyncPanel } from "./GmailSyncPanel";
import { MessagesSyncPanel } from "./MessagesSyncPanel";
import {
  fetchSetupJob,
  fetchSetupStatus,
  runOnboardingV3LinkedIn,
  runSetupAction,
  uploadLinkedInCsv,
} from "./powerpacksApi";
import type { SetupEnrichmentSource, SetupImportSource, SetupSourceStatus, SetupStatusResponse } from "./types";

function formatDate(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function StatCard({ label, value, hint, loading }: { label: string; value: string; hint: string; loading?: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-lg">
          {loading ? <div className="h-6 w-16 animate-pulse rounded-md bg-muted" /> : value}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="h-3 w-28 animate-pulse rounded bg-muted" />
        ) : (
          <p className="text-xs text-muted-foreground">{hint}</p>
        )}
      </CardContent>
    </Card>
  );
}

function ConnectionBadge({ source, loading }: { source?: SetupSourceStatus; loading?: boolean }) {
  if (loading) {
    return <div className="h-6 w-24 animate-pulse rounded-full bg-muted" />;
  }
  if (source?.linked) {
    return (
      <Badge className="gap-1 bg-emerald-600 hover:bg-emerald-600">
        <CheckCircle2 className="h-3 w-3" /> Connected
      </Badge>
    );
  }
  return <Badge variant="outline" className="text-muted-foreground">Not connected</Badge>;
}

function useSetupStatus() {
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const refresh = useCallback(async () => {
    try {
      setStatus(await fetchSetupStatus());
    } catch {
      /* best-effort poll */
    }
  }, []);
  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  return { status, refresh };
}

// Run a setup-action job (re-import / re-enrich), poll to completion, refresh.
function useSourceJob(refresh: () => void) {
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (key: string, body: Record<string, unknown>) => {
      setRunning(key);
      setError(null);
      try {
        const { job } = await runSetupAction(body);
        let current = job;
        while (current.status === "running" || current.status === "pending") {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          current = await fetchSetupJob(job.id);
        }
        if (current.status !== "completed") setError(current.stderr || "That run did not complete.");
        refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Run failed");
      } finally {
        setRunning(null);
      }
    },
    [refresh]
  );
  return { running, error, run };
}

function SourceHeader({ icon, title, description }: { icon: React.ReactNode; title: string; description: string }) {
  return (
    <div className="mb-6 flex items-start gap-3">
      <div className="shrink-0">{icon}</div>
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
    </div>
  );
}

const GMAIL_ICON = (
  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#EA4335]/10">
    <svg viewBox="0 0 24 24" className="h-5 w-5 text-[#EA4335]" fill="currentColor">
      <path d="M22 5.5v13a1.5 1.5 0 0 1-1.5 1.5H19V8.7l-7 5.25L5 8.7V20H3.5A1.5 1.5 0 0 1 2 18.5v-13l1.2-.9L12 11.25l8.8-6.65 1.2.9Z" />
    </svg>
  </span>
);

const LINKEDIN_ICON = (
  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#0A66C2]/10 text-base font-bold text-[#0A66C2]">
    in
  </span>
);

const MESSAGES_ICON = (
  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#15803D]/10">
    <MessageCircle className="h-5 w-5 text-[#15803D]" />
  </span>
);

export function GmailSourcePage() {
  const { status, refresh } = useSetupStatus();
  const { running, error, run } = useSourceJob(refresh);

  const loading = !status;
  const accountSource = status?.accounts.sources.find((s: SetupSourceStatus) => s.id === "gmail");
  const enrich = status?.enrichment.sources.find((s: SetupEnrichmentSource) => s.id === "gmail");
  const imp = status?.import.sources.find((s: SetupImportSource) => s.sourceId === "gmail");

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between gap-3">
        <SourceHeader icon={GMAIL_ICON} title="Gmail" description="Sync the people you email and enrich them into your network." />
        <ConnectionBadge source={accountSource} loading={loading} />
      </div>

      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <StatCard
          loading={loading}
          label="Contacts discovered"
          value={enrich?.candidates ? enrich.candidates.toLocaleString() : "—"}
          hint={imp?.accountCount ? `Across ${imp.accountCount} account${imp.accountCount === 1 ? "" : "s"}` : "From your synced email"}
        />
        <StatCard
          loading={loading}
          label="Enriched"
          value={enrich?.enriched ? enrich.enriched.toLocaleString() : "—"}
          hint={enrich?.matched ? `${enrich.matched.toLocaleString()} matched to profiles` : "Run enrich to resolve profiles"}
        />
        <StatCard loading={loading} label="Last sync" value={formatDate(accountSource?.lastSuccessAt)} hint={imp?.status ? `Import ${imp.status}` : "Sync below to update"} />
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Sync email</CardTitle>
          <CardDescription>Pick how far back to pull. Newsletters, promotions and social are skipped.</CardDescription>
        </CardHeader>
        <CardContent>
          <GmailSyncPanel onChange={refresh} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Enrich</CardTitle>
          <CardDescription>Resolve your synced contacts into full profiles, then rebuild the local index.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            disabled={!accountSource?.linked || running !== null}
            onClick={() => run("enrich", { action: "enrich-source", source: "gmail", approveSpend: true })}
          >
            {running === "enrich" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
            Re-run enrich
          </Button>
          <Button
            variant="outline"
            disabled={running !== null}
            onClick={() => run("index", { action: "index" })}
          >
            {running === "index" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Rebuild index
          </Button>
          {error && <p className="w-full text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
    </div>
  );
}

export function LinkedInSourcePage() {
  const { status, refresh } = useSetupStatus();
  const { running, error, run } = useSourceJob(refresh);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const loading = !status;
  const accountSource = status?.accounts.sources.find((s: SetupSourceStatus) => s.id === "linkedin_csv");
  const enrich = status?.enrichment.sources.find((s: SetupEnrichmentSource) => s.id === "linkedin_csv");

  async function handleUpload(file?: File | null) {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      const { path } = await uploadLinkedInCsv(file);
      // runOnboardingV3LinkedIn only STARTS the Modal import job. Poll it to
      // completion so the button stays "Importing…" the whole time and we
      // refresh (and surface failures) only once the import actually finished
      // and wrote accounts.json/stats — a single early refresh showed nothing.
      const { job } = await runOnboardingV3LinkedIn({ csvPath: path });
      let current = job;
      while (current.status === "running" || current.status === "pending") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        current = await fetchSetupJob(job.id);
      }
      if (current.status !== "completed") setUploadError(current.stderr || "Import did not complete.");
      refresh();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between gap-3">
        <SourceHeader icon={LINKEDIN_ICON} title="LinkedIn" description="Import and enrich your LinkedIn connections." />
        <ConnectionBadge source={accountSource} loading={loading} />
      </div>

      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <StatCard
          loading={loading}
          label="Connections"
          value={enrich?.candidates ? enrich.candidates.toLocaleString() : "—"}
          hint="From your Connections.csv"
        />
        <StatCard
          loading={loading}
          label="Enriched"
          value={enrich?.enriched ? enrich.enriched.toLocaleString() : "—"}
          hint={enrich?.matched ? `${enrich.matched.toLocaleString()} matched to profiles` : "Run enrich to resolve profiles"}
        />
        <StatCard loading={loading} label="Last import" value={formatDate(accountSource?.lastSuccessAt)} hint={accountSource?.linked ? "Connected" : "Upload a CSV to start"} />
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Import connections</CardTitle>
          <CardDescription>
            LinkedIn → Settings → Data privacy → Get a copy of your data → Connections.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <input ref={fileInputRef} type="file" accept=".csv" className="hidden" onChange={(e) => handleUpload(e.target.files?.[0])} />
          <Button disabled={uploading} onClick={() => fileInputRef.current?.click()}>
            {uploading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
            {uploading ? "Importing…" : accountSource?.linked ? "Re-upload Connections.csv" : "Upload Connections.csv"}
          </Button>
          {accountSource?.linked && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
              <FileCheck2 className="h-3.5 w-3.5" /> Connections imported. Re-upload to refresh.
            </p>
          )}
          {uploadError && <p className="mt-2 text-sm text-destructive">{uploadError}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Enrich</CardTitle>
          <CardDescription>Resolve your connections into full profiles, then rebuild the local index.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            disabled={!accountSource?.linked || running !== null}
            onClick={() => run("enrich", { action: "enrich-source", source: "linkedin_csv", approveSpend: true })}
          >
            {running === "enrich" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
            Re-run enrich
          </Button>
          <Button variant="outline" disabled={running !== null} onClick={() => run("index", { action: "index" })}>
            {running === "index" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Rebuild index
          </Button>
          {error && <p className="w-full text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
    </div>
  );
}

export function MessagesSourcePage() {
  const { status, refresh } = useSetupStatus();
  const { running, error, run } = useSourceJob(refresh);

  const loading = !status;
  const accountSource = status?.accounts.sources.find((s: SetupSourceStatus) => s.id === "messages");
  const enrich = status?.enrichment.sources.find((s: SetupEnrichmentSource) => s.id === "messages");
  const imp = status?.import.sources.find((s: SetupImportSource) => s.sourceId === "messages");

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between gap-3">
        <SourceHeader
          icon={MESSAGES_ICON}
          title="Messages"
          description="Sync the people you iMessage and WhatsApp, then enrich them into your network."
        />
        <ConnectionBadge source={accountSource} loading={loading} />
      </div>

      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <StatCard
          loading={loading}
          label="Contacts discovered"
          value={enrich?.candidates ? enrich.candidates.toLocaleString() : "—"}
          hint="From your conversations"
        />
        <StatCard
          loading={loading}
          label="In network"
          value={enrich?.enriched ? enrich.enriched.toLocaleString() : "—"}
          hint={enrich?.candidates ? "Approved and enriched" : "Approve contacts to enrich"}
        />
        <StatCard
          loading={loading}
          label="Last import"
          value={formatDate(accountSource?.lastSuccessAt)}
          hint={imp?.status ? `Import ${imp.status}` : "Sync below to update"}
        />
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Sync messages</CardTitle>
          <CardDescription>Link iMessage and WhatsApp, then review who&apos;s worth enriching. No message contents are read.</CardDescription>
        </CardHeader>
        <CardContent>
          <MessagesSyncPanel onChange={refresh} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Enrich</CardTitle>
          <CardDescription>Resolve your approved contacts into full profiles, then rebuild the local index.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            disabled={!accountSource?.linked || running !== null}
            onClick={() => run("enrich", { action: "enrich-source", source: "messages", approveSpend: true })}
          >
            {running === "enrich" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
            Re-run enrich
          </Button>
          <Button variant="outline" disabled={running !== null} onClick={() => run("index", { action: "index" })}>
            {running === "index" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Rebuild index
          </Button>
          {error && <p className="w-full text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
    </div>
  );
}
