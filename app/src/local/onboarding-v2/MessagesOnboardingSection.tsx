import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, DollarSign, ExternalLink, Loader2, MessageSquare, ShieldAlert, Smartphone } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchOnboardingV2MessagesStatus,
  runOnboardingV2Messages,
  runSetupAction,
} from "../powerpacksApi";
import type { SetupJob } from "../types";
import { OnboardingStatusCard } from "./OnboardingStatusCard";
import {
  commandText,
  numberValue,
  objectValue,
  stringValue,
  type JsonObject,
} from "./utils";

const MESSAGES_DEFAULT_STAGES = [
  { id: "inspect", label: "Check message sources" },
  { id: "discover", label: "Discover message contacts" },
  { id: "llm_review", label: "AI contact review" },
  { id: "user_review", label: "Review contacts" },
  { id: "enrich", label: "Enrich message contacts" },
  { id: "source_people", label: "Save message people file" },
  { id: "merge_network", label: "Merge contact sources" },
  { id: "network_duckdb", label: "Prepare contact lookup database" },
  { id: "index_estimate", label: "Estimate search updates" },
  { id: "index_records", label: "Build searchable people records" },
  { id: "search_duckdb", label: "Update local search database" },
];

export function MessagesOnboardingSection() {
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [latestJob, setLatestJob] = useState<SetupJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [linking, setLinking] = useState(false);
  const [whatsappLinking, setWhatsappLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchOnboardingV2MessagesStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load messages onboarding status");
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const currentStatus = stringValue(status?.status);

  // Source link status
  const sources = objectValue(status?.sources);
  const imessage = objectValue(sources.imessage);
  const whatsapp = objectValue(sources.whatsapp);
  const imessageReady = stringValue(imessage.status) === "ready";
  const whatsappAuthenticated = stringValue(whatsapp.status) === "authenticated";
  const hasSources = imessageReady || whatsappAuthenticated;
  const whatsappQrPath = stringValue(whatsapp.qr_png);
  const whatsappQrUpdatedAt = stringValue(whatsapp.qr_updated_at);
  const whatsappQrSrc = whatsappQrPath
    ? `/local-api/setup/whatsapp-qr?path=${encodeURIComponent(whatsappQrPath)}${whatsappQrUpdatedAt ? `&t=${encodeURIComponent(whatsappQrUpdatedAt)}` : ""}`
    : "";

  // Clear linking spinner once authenticated
  useEffect(() => {
    if (whatsappAuthenticated && whatsappLinking) setWhatsappLinking(false);
  }, [whatsappAuthenticated, whatsappLinking]);

  // Blocked states
  const userReviewStage = objectValue(objectValue(status?.stages).user_review);
  const enrichStage = objectValue(objectValue(status?.stages).enrich);
  const isBlockedReview = currentStatus === "blocked_user_action"
    || stringValue(userReviewStage.status) === "blocked_user_action";
  const isBlockedApproval = currentStatus === "blocked_approval"
    || stringValue(enrichStage.status) === "blocked_approval";
  const blockedSpend = objectValue(
    objectValue(enrichStage.payload).parallel_spend_estimate
    || status?.parallel_spend_estimate
  );
  const blockedPending = numberValue(blockedSpend.pending_contacts);
  const blockedCost = numberValue(blockedSpend.estimated_usd);

  const reviewPayload = objectValue(userReviewStage.payload);
  const reviewCounts = objectValue(reviewPayload.review_counts);
  const reviewTotal = numberValue(reviewCounts.total);

  const latestCommand = commandText(latestJob?.command);
  const latestOutput = latestJob?.output || null;
  const latestStdout = stringValue(latestJob?.stdout);
  const latestStderr = stringValue(latestJob?.stderr);

  async function handleRun() {
    setLoading(true);
    setError(null);
    try {
      const response = await runOnboardingV2Messages({});
      setLatestJob(response.job);
      setStatus(response.status);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleContinueAfterReview() {
    setLoading(true);
    setError(null);
    try {
      const response = await runOnboardingV2Messages({ continueRun: true });
      setLatestJob(response.job);
      setStatus(response.status);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleApproveSpend() {
    setLoading(true);
    setError(null);
    try {
      const response = await runOnboardingV2Messages({ approveSpend: true, continueRun: true });
      setLatestJob(response.job);
      setStatus(response.status);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleAction(action: string, body: Record<string, unknown> = {}) {
    setLinking(true);
    setError(null);
    const minSpinner = new Promise((r) => setTimeout(r, 1000));
    try {
      await runSetupAction({ action, ...body });
      await minSpinner;
      await loadStatus();
    } catch (err) {
      await minSpinner;
      setError(err instanceof Error ? err.message : `${action} failed`);
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
            <MessageSquare className="h-4 w-4" /> Messages
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Import contacts from iMessage and WhatsApp. AI reviews who&apos;s worth enriching, then you approve before Parallel.ai lookups.
          </p>

          {/* Source status */}
          <div className="rounded-lg border bg-muted/30 p-3 text-sm">
            <div className="text-muted-foreground">Message sources</div>
            <div className="mt-2 space-y-2">
              {/* iMessage */}
              <div className="flex items-center gap-2">
                {imessageReady ? (
                  <>
                    <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                    <span className="font-medium">iMessage</span>
                    <span className="text-xs text-emerald-600">Ready</span>
                  </>
                ) : (
                  <>
                    <ShieldAlert className="h-4 w-4 shrink-0 text-amber-600" />
                    <span className="font-medium">iMessage</span>
                    <span className="text-xs text-amber-600">Full Disk Access required</span>
                  </>
                )}
                {imessageReady ? null : (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={linking}
                    onClick={() => handleAction("open-message-permissions")}
                  >
                    {linking ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                    Check File Access
                  </Button>
                )}
              </div>

              {/* WhatsApp */}
              <div className="flex items-center gap-2">
                {whatsappAuthenticated ? (
                  <>
                    <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                    <span className="font-medium">WhatsApp</span>
                    <span className="text-xs text-emerald-600">Authenticated</span>
                  </>
                ) : (
                  <>
                    <Smartphone className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="font-medium">WhatsApp</span>
                    <span className="text-xs text-muted-foreground">Not linked</span>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={linking || whatsappLinking}
                      onClick={async () => {
                        setWhatsappLinking(true);
                        setError(null);
                        try {
                          await runSetupAction({ action: "whatsapp-auth" });
                        } catch (err) {
                          setError(err instanceof Error ? err.message : "WhatsApp auth failed");
                        }
                        // Don't clear whatsappLinking — polling will show QR then authenticated
                      }}
                    >
                      {whatsappLinking ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                      Link WhatsApp
                    </Button>
                  </>
                )}
              </div>

              {/* WhatsApp QR code or generating spinner */}
              {!whatsappAuthenticated && (whatsappQrSrc || whatsappLinking) && (
                <div className="mt-2 rounded-md border bg-muted/20 p-4">
                  <div className="mb-2">
                    <div className="text-sm font-medium">Scan in WhatsApp</div>
                    <div className="text-xs text-muted-foreground">WhatsApp → Settings → Linked Devices</div>
                  </div>
                  {whatsappQrSrc ? (
                    <img
                      src={whatsappQrSrc}
                      alt="WhatsApp QR code"
                      className="mx-auto block w-full max-w-[360px] rounded-md border bg-white p-4"
                    />
                  ) : (
                    <div className="flex items-center justify-center gap-3 py-8 text-sm text-muted-foreground">
                      <Loader2 className="h-5 w-5 animate-spin" />
                      <span>Generating QR code…</span>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Review gate */}
          {isBlockedReview && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
              <div className="font-medium text-amber-900">Review your contacts</div>
              <p className="mt-1 text-amber-800">
                {reviewTotal} contact{reviewTotal !== 1 ? "s" : ""} ready for review. Approve or reject contacts, then continue.
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" asChild>
                  <a href="/setup/imessage/review" target="_blank" rel="noopener noreferrer">
                    <ExternalLink className="mr-2 h-4 w-4" />
                    Open review
                  </a>
                </Button>
                <Button
                  size="sm"
                  disabled={loading}
                  onClick={handleContinueAfterReview}
                >
                  {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  Done reviewing — continue
                </Button>
              </div>
            </div>
          )}

          {/* Approval gate */}
          {isBlockedApproval && blockedPending > 0 && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
              <div className="font-medium text-amber-900">Approval needed</div>
              <p className="mt-1 text-amber-800">
                {blockedPending} contact{blockedPending !== 1 ? "s" : ""} need Parallel.ai resolution.
                Estimated cost: <span className="font-semibold">${blockedCost.toFixed(2)}</span>
              </p>
              <div className="mt-2">
                <Button
                  size="sm"
                  disabled={loading}
                  onClick={handleApproveSpend}
                >
                  {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <DollarSign className="mr-2 h-4 w-4" />}
                  Approve ${blockedCost.toFixed(2)} and continue
                </Button>
              </div>
            </div>
          )}

          {/* Action button */}
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={loading || !hasSources} onClick={handleRun}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null} Run Messages v2
            </Button>
            {!hasSources && (
              <span className="text-xs text-muted-foreground">Link at least one message source above to start</span>
            )}
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
      <OnboardingStatusCard status={status} defaultStages={MESSAGES_DEFAULT_STAGES} />
    </div>
  );
}
