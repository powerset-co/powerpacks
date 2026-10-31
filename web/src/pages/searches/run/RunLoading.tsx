import { Skeleton } from "@/components/ui/skeleton"

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
      <RowsSkeleton />
    </div>
  )
}

// Each placeholder row's line widths (name, title, company, reasoning's second line), so the
// rows read as different people; later rows fade out.
const ROW_SHAPES = [
  [44, 62, 54, 70],
  [52, 40, 66, 48],
  [36, 58, 46, 82],
  [48, 50, 60, 56],
  [40, 66, 50, 64],
  [56, 44, 58, 52],
] as const

/** Placeholder result rows shaped as the real ones (rendering.py candidate rows): the person
 *  on the left, a score and its reasoning on the right. While a run loads, and while a running
 *  search has none to list yet. */
export function RowsSkeleton() {
  return (
    <div className="grid content-start" aria-hidden>
      {ROW_SHAPES.map(([name, title, company, reason], index) => (
        <div
          key={index}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 border-b border-line px-3 py-4"
          style={{ opacity: 1 - index * 0.15 }}
        >
          <div className="flex gap-[11px] pt-[26px]">
            <Skeleton className="size-[34px] shrink-0 rounded-full" />
            <div className="grid min-w-0 flex-1 content-start gap-2 pt-1">
              <Skeleton className="h-3.5" style={{ width: `${name}%` }} />
              <Skeleton className="h-3" style={{ width: `${title}%` }} />
              <Skeleton className="h-2.5" style={{ width: `${company}%` }} />
              <Skeleton className="mt-1 h-5 w-10 rounded-full" />
            </div>
          </div>
          <div className="flex gap-2">
            <Skeleton className="h-5 w-11 shrink-0 rounded-full" />
            <div className="grid flex-1 content-start gap-2 pt-1">
              <Skeleton className="h-3 w-[92%]" />
              <Skeleton className="h-3" style={{ width: `${reason}%` }} />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
