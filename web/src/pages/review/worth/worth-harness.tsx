import { useMemo, useState } from "react"
import { useLocation } from "react-router-dom"

import { ReviewHarness } from "@/testing/review-harness"
import type { DecisionProgress, WorthTab } from "@/types/review"

import type { Review } from "../hooks/useReview"
import { WorthStage } from "./WorthStage"

interface WorthHarnessProps {
  tab: WorthTab
  /** The page's `Review` as the stage starts with it. */
  review: Review
  /** The address the stage renders at. */
  path: string
}

function Where() {
  const location = useLocation()
  return <span data-where>{location.pathname + location.search}</span>
}

// The worth stage as a test sees it: inside a `Review` whose counts move as the page's do
// (`applyProgress` repaints them), beside a probe that shows the address its links opened.
export function WorthHarness({ tab, review, path }: WorthHarnessProps) {
  const [applied, setApplied] = useState<Partial<DecisionProgress>>({})
  const live = useMemo(
    (): Review => ({
      ...review,
      progress: { ...review.progress, ...applied },
      applyProgress: (progress) => {
        review.applyProgress(progress)
        setApplied((before) => ({ ...before, ...progress }))
      },
    }),
    [review, applied],
  )
  return (
    <ReviewHarness review={live} path={path}>
      <WorthStage tab={tab} />
      <Where />
    </ReviewHarness>
  )
}
