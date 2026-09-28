import type { LastUpload, UploadStatus } from "@/lib/api/upload"
import { countOf } from "@/lib/copy"
import { finishedAt, UPLOAD } from "@/lib/people/copy"
import { isActive } from "@/lib/people/upload"

interface UploadProgressProps {
  status: UploadStatus | undefined
  reconnecting: boolean
}

/** One message line and one bar counting people: indeterminate until the upload has a total. */
export function UploadProgress({ status, reconnecting }: UploadProgressProps) {
  const live = status && isActive(status.status) ? status : undefined
  const message = reconnecting ? UPLOAD.reconnecting : (live?.message ?? UPLOAD.starting)
  const counting = live?.status === "uploading" && live.progress.total > 0 ? live.progress : null
  const done = counting ? counting.uploaded + counting.skipped : 0
  return (
    <div className="grid gap-2 text-[13px]" aria-live="polite">
      <div className="flex items-baseline justify-between gap-3">
        <span>{message}</span>
        {counting && (
          <span className="tabular-nums text-muted-foreground">
            {countOf(done, counting.total, "person")}
          </span>
        )}
      </div>
      <div
        role="progressbar"
        aria-label={UPLOAD.progress}
        aria-valuemin={0}
        aria-valuemax={counting?.total}
        aria-valuenow={counting ? done : undefined}
        className="h-1.5 overflow-hidden rounded-full bg-secondary"
      >
        {counting ? (
          <div
            className="h-full origin-left bg-primary transition-transform duration-med ease-out"
            style={{ transform: `scaleX(${done / counting.total})` }}
          />
        ) : (
          <div className="h-full w-full -translate-x-full animate-[shimmer_var(--t-shimmer)_var(--ease-in)_infinite] bg-[linear-gradient(90deg,transparent,var(--primary),transparent)] motion-reduce:translate-x-0 motion-reduce:opacity-40" />
        )}
      </div>
    </div>
  )
}

/** After a run: what it wrote, what was already current, and when it finished. */
export function UploadDone({ last }: { last: LastUpload }) {
  return (
    <div className="grid gap-3">
      <dl className="m-0 grid grid-cols-2 gap-4">
        {[
          { label: UPLOAD.uploaded, value: last.uploaded },
          { label: UPLOAD.upToDate, value: last.skipped },
        ].map((row) => (
          <div key={row.label}>
            <dt className="text-xs text-muted-foreground">{row.label}</dt>
            <dd className="m-0 mt-1 text-2xl font-semibold tabular-nums">{row.value.toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      <p className="m-0 text-xs text-muted-foreground">{finishedAt(last.finished_at)}</p>
    </div>
  )
}
