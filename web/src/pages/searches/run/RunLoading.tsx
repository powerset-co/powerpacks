import { Skeleton } from "@/components/ui/skeleton"

const SKELETON_ROWS = 8

// The run's frame while it loads: a header, the pond chain and shimmering rows. It fades in
// only after --t-slow, so a quick load goes straight from the old run to the new one.
export function RunLoading() {
  return (
    <div className="search-run run-waiting" aria-busy="true">
      <span className="sr-only">Loading search…</span>
      <div className="run-head">
        <div className="grid gap-2">
          <Skeleton className="h-3 w-28" />
          <Skeleton className="h-5 w-72" />
          <Skeleton className="h-3 w-56" />
        </div>
      </div>
      <div className="pond-chain">
        <div className="flex gap-3">
          <Skeleton className="h-[58px] w-60" />
          <Skeleton className="h-[58px] w-60" />
        </div>
      </div>
      <div className="grid content-start gap-px px-5 pt-3">
        {Array.from({ length: SKELETON_ROWS }, (_, index) => (
          <Skeleton key={index} className="h-[51px] rounded-none" />
        ))}
      </div>
    </div>
  )
}
