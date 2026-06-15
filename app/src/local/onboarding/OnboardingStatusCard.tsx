import { CheckCircle2, CircleAlert, CircleDot, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { arrayValue, numberValue, objectValue, statusTone, stringValue, type JsonObject } from "./utils";

function stageRows(status: JsonObject | null, defaultStages: { id: string; label: string }[]) {
  const order = arrayValue(status?.stage_order);
  const stages = objectValue(status?.stages);
  const activeOrder = order.length > 0 ? order : defaultStages;
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

export function OnboardingStatusCard({ status, defaultStages }: { status: JsonObject | null; defaultStages: { id: string; label: string }[] }) {
  const statusText = stringValue(status?.status || "missing");
  const progress = Math.max(0, Math.min(1, numberValue(status?.progress)));
  const steps = stageRows(status, defaultStages);
  const outputs = objectValue(objectValue(status?.result).outputs || status?.outputs);
  return (
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
        <div className="grid gap-2 text-sm md:grid-cols-2">
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
  );
}
