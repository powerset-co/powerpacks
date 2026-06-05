import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, KeyRound, Loader2, RefreshCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { fetchEnvStatus } from "./powerpacksApi";
import type { EnvKeyStatus, EnvStatusResponse } from "./types";

function envTone(key: EnvKeyStatus): "default" | "secondary" | "destructive" | "outline" {
  if (key.satisfied) return "default";
  if (key.required) return "destructive";
  if (key.status === "empty") return "secondary";
  return "outline";
}

function envLabel(key: EnvKeyStatus): string {
  if (key.status === "present_via_alias" && key.satisfiedBy) return `set via ${key.satisfiedBy}`;
  if (key.satisfied) return "set";
  if (key.status === "empty") return "empty";
  return "missing";
}

function EnvRow({ item }: { item: EnvKeyStatus }) {
  const warning = !item.satisfied;
  const aliasText = (item.aliases || []).map((alias) => alias.key).join(", ");
  return (
    <div className="grid gap-3 border-b px-4 py-3 last:border-b-0 lg:grid-cols-[minmax(0,1.2fr)_120px_160px_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {warning ? (
            <AlertTriangle className={cn("h-4 w-4", item.required ? "text-destructive" : "text-amber-600")} />
          ) : (
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          )}
          <div className="truncate font-mono text-sm font-medium">{item.key}</div>
          {item.required && <Badge variant="secondary">Required</Badge>}
        </div>
        <div className="mt-1 text-sm text-muted-foreground">{item.description}</div>
        {aliasText && (
          <div className="mt-1 text-xs text-muted-foreground">Fallback: {aliasText}</div>
        )}
      </div>
      <div className="text-sm text-muted-foreground">{item.provider}</div>
      <div>
        <Badge variant={envTone(item)}>{envLabel(item)}</Badge>
        {item.valuePreview && <span className="ml-2 font-mono text-xs text-muted-foreground">{item.valuePreview}</span>}
      </div>
      <div className="flex justify-end">
        {!item.satisfied && item.getUrl && (
          <Button size="sm" variant="outline" onClick={() => window.open(item.getUrl, "_blank", "noopener,noreferrer")}>
            <ExternalLink className="h-4 w-4" /> Get API Key
          </Button>
        )}
      </div>
    </div>
  );
}

export function LocalEnvPage() {
  const [status, setStatus] = useState<EnvStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setStatus(await fetchEnvStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load env status");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const missingRequired = useMemo(() => (status?.keys || []).filter((key) => key.required && !key.satisfied), [status]);

  if (isLoading && !status) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" /> Loading environment
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">Environment</h2>
          <p className="mt-1 text-sm text-muted-foreground">{status?.path || ".env"}</p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={isLoading}>
          <RefreshCcw className="h-4 w-4" /> Refresh
        </Button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {status && !status.exists && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>No `.env` file was found in this Powerpacks checkout.</div>
          </div>
        </div>
      )}

      {status && missingRequired.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <span className="font-medium">{missingRequired.length} required key{missingRequired.length === 1 ? "" : "s"} need attention.</span>{" "}
              Add them to `.env`, then refresh this page.
            </div>
          </div>
        </div>
      )}

      {status && (
        <section className="rounded-md border bg-card">
          <div className="flex flex-wrap items-center gap-2 border-b p-4">
            <KeyRound className="h-4 w-4 text-muted-foreground" />
            <h3 className="text-base font-semibold">API Keys</h3>
            <Badge variant={status.summary.ready ? "default" : "secondary"}>
              {status.summary.present}/{status.summary.total} set
            </Badge>
          </div>
          <div>
            {status.keys.map((item) => (
              <EnvRow key={item.key} item={item} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
