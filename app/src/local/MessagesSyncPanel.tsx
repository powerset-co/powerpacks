import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, DollarSign, ExternalLink, Loader2, MessageSquare, ShieldAlert, Smartphone } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  fetchOnboardingV2MessagesStatus,
  runOnboardingV2Messages,
  runSetupAction,
} from "./powerpacksApi";
import { numberValue, objectValue, stringValue } from "./onboarding-v2/utils";

/**
 * iMessage/WhatsApp readiness + import pipeline for the Messages source page.
 * Mirrors GmailSyncPanel's role (link sources, then sync) but adapts to the
 * messages realities: Full Disk Access for iMessage, WhatsApp QR auth, and the
 * in-app review/approval (`in_network`) gates that Gmail doesn't have. Calls
 * onChange after a pipeline run so a parent can refresh its own stats. No
 * message contents are ever read — only contact metadata.
 */
export function MessagesSyncPanel({ onChange }: { onChange?: () => void } = {}) {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false); // pipeline run in flight
  const [linking, setLinking] = useState(false); // Full Disk Access check
  const [whatsappLinking, setWhatsappLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchOnboardingV2MessagesStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load messages status");
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 2000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const currentStatus = stringValue(status?.status);
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

  // Clear the WhatsApp spinner once polling reports authenticated.
  useEffect(() => {
    if (whatsappAuthenticated && whatsappLinking) setWhatsappLinking(false);
  }, [whatsappAuthenticated, whatsappLinking]);

  // Review/approval gates — the messages pipeline pauses for user action.
  const userReviewStage = objectValue(objectValue(status?.stages).user_review);
  const enrichStage = objectValue(objectValue(status?.stages).enrich);
  const isBlockedReview = currentStatus === "blocked_user_action"
    || stringValue(userReviewStage.status) === "blocked_user_action";
  const isBlockedApproval = currentStatus === "blocked_approval"
    || stringValue(enrichStage.status) === "blocked_approval";
  const blockedSpend = objectValue(
    objectValue(enrichStage.payload).parallel_spend_estimate || status?.parallel_spend_estimate
  );
  const blockedPending = numberValue(blockedSpend.pending_contacts);
  const blockedCost = numberValue(blockedSpend.estimated_usd);
  const reviewPayload = objectValue(userReviewStage.payload);
  const reviewCounts = objectValue(reviewPayload.review_counts);
  const reviewTotal = numberValue(reviewCounts.total);

  // Surface a failed background run — otherwise the panel showed nothing when a
  // stage failed (it only rendered the review/approval gates and live errors).
  // The pipeline runs as a background job; the panel polls status. Reflect a
  // running run on the button so it doesn't look idle while work is happening.
  const running = currentStatus === "running" || Boolean(status?.active_job);
  const failed = currentStatus === "failed";
  const failedEntry = failed
    ? Object.entries(objectValue(status?.stages)).find(([, v]) => stringValue(objectValue(v).status) === "failed")
    : undefined;
  const failedStageLabel = failedEntry ? stringValue(objectValue(failedEntry[1]).label) || failedEntry[0] : "";
  const failedMessage = failedEntry ? stringValue(objectValue(failedEntry[1]).message) : "";

  async function runPipeline(body: Record<string, unknown>) {
    setLoading(true);
    setError(null);
    try {
      const response = await runOnboardingV2Messages(body);
      setStatus(response.status);
      await loadStatus();
      onChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setLoading(false);
    }
  }

  async function checkFileAccess() {
    setLinking(true);
    setError(null);
    const minSpinner = new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      await runSetupAction({ action: "open-message-permissions" });
      await minSpinner;
      await loadStatus();
    } catch (err) {
      await minSpinner;
      setError(err instanceof Error ? err.message : "File access check failed");
    } finally {
      setLinking(false);
    }
  }

  async function linkWhatsapp() {
    setWhatsappLinking(true);
    setError(null);
    try {
      await runSetupAction({ action: "whatsapp-auth" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "WhatsApp auth failed");
    }
    // Leave the spinner up — polling renders the QR, then flips to authenticated.
  }

  return (
    <div className="space-y-3">
      {/* Message sources */}
      <div className="rounded-lg border bg-muted/30 p-3">
        <span className="text-sm font-medium">Message sources</span>
        <div className="mt-2 space-y-2">
          {/* iMessage */}
          <div className="flex items-center gap-2 text-sm">
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
                <Button size="sm" variant="outline" className="ml-auto" disabled={linking} onClick={checkFileAccess}>
                  {linking ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                  Check File Access
                </Button>
              </>
            )}
          </div>

          {/* WhatsApp */}
          <div className="flex items-center gap-2 text-sm">
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
                <Button size="sm" variant="outline" className="ml-auto" disabled={whatsappLinking} onClick={linkWhatsapp}>
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

      <p className="text-sm text-muted-foreground">
        We sync the people you actually message — AI reviews who&apos;s worth enriching, then you approve before any
        paid lookups. No message contents are ever read.
      </p>

      {/* Review gate */}
      {isBlockedReview && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
          <div className="font-medium text-amber-900">Review your contacts</div>
          <p className="mt-1 text-amber-800">
            {reviewTotal} contact{reviewTotal !== 1 ? "s" : ""} ready for review. Approve or reject, then continue.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" asChild>
              <a href="/setup/imessage/review" target="_blank" rel="noopener noreferrer">
                <ExternalLink className="mr-2 h-4 w-4" />
                Open review
              </a>
            </Button>
            <Button size="sm" disabled={loading} onClick={() => runPipeline({ continueRun: true })}>
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
            {blockedPending} contact{blockedPending !== 1 ? "s" : ""} need Parallel.ai resolution. Estimated cost:{" "}
            <span className="font-semibold">${blockedCost.toFixed(2)}</span>
          </p>
          <div className="mt-2">
            <Button size="sm" disabled={loading} onClick={() => runPipeline({ approveSpend: true, continueRun: true })}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <DollarSign className="mr-2 h-4 w-4" />}
              Approve ${blockedCost.toFixed(2)} and continue
            </Button>
          </div>
        </div>
      )}

      {failed && !loading && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          Sync failed{failedStageLabel ? ` at "${failedStageLabel}"` : ""}
          {failedMessage ? `: ${failedMessage}` : ""}. Press Sync messages to retry.
        </div>
      )}

      <Button className="w-full" disabled={loading || running || !hasSources} onClick={() => runPipeline({})}>
        {loading || running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <MessageSquare className="mr-2 h-4 w-4" />}
        {loading || running ? "Syncing messages…" : failed ? "Retry sync" : "Sync messages"}
      </Button>
      {!hasSources && (
        <p className="text-center text-xs text-muted-foreground">Link iMessage or WhatsApp above to start.</p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
