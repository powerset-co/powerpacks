import { Skeleton } from "@/components/ui/skeleton"
import type { UploadPlan } from "@/lib/api/upload"
import { PLAN_ALWAYS, PLAN_ROWS } from "@/lib/people/copy"

/** The check's plan as a compact list; zero rows are hidden except what will upload. */
export function UploadSummary({ plan }: { plan: UploadPlan }) {
  const rows = PLAN_ROWS.filter((row) => row.key === PLAN_ALWAYS || plan[row.key] > 0)
  return (
    <dl className="m-0 grid grid-cols-[1fr_auto] gap-x-6 gap-y-1.5 text-[13px]">
      {rows.map((row) => (
        <div key={row.key} className="contents">
          <dt className="text-muted-foreground">{row.label}</dt>
          <dd className="m-0 text-right font-semibold tabular-nums">{plan[row.key].toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  )
}

/** The plan's shape while the check runs, so the numbers land without the dialog shifting. */
export function UploadSummarySkeleton() {
  return (
    <div aria-hidden="true" className="grid grid-cols-[1fr_auto] gap-x-6 gap-y-1.5">
      {[0, 1, 2].map((row) => (
        <div key={row} className="contents">
          <Skeleton className="my-[3px] h-3.5 w-40 bg-surface-2" />
          <Skeleton className="my-[3px] h-3.5 w-8 bg-surface-2" />
        </div>
      ))}
    </div>
  )
}
