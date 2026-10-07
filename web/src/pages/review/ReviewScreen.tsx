import { cn } from "@/lib/utils"

import { ReviewContext } from "./hooks/useReview"
import type { ReviewToast } from "./hooks/useReviewToast"
import type { Screen } from "./hooks/useScreen"
import { useScreenReview } from "./hooks/useScreenReview"
import { LinkedinStage } from "./linkedin/LinkedinStage"
import { SynthesisPending } from "./shared/SynthesisPending"

interface ReviewScreenProps {
  screen: Screen
  toast: ReviewToast
  reload: () => void
}

// One loaded screen: the Check LinkedIn stage inside the `Review` it works through. The page
// keys it by the load, so it starts over (and fades in) each time.
export function ReviewScreen({ screen, toast, reload }: ReviewScreenProps) {
  const { page } = screen
  const { review, leaving } = useScreenReview({ screen, toast, reload })
  return (
    <main className="review-main">
      <section className={cn("stage", leaving && "leaving")} aria-live="polite">
        <ReviewContext.Provider value={review}>
          {page.needs_synthesis ? <SynthesisPending /> : <LinkedinStage />}
        </ReviewContext.Provider>
      </section>
    </main>
  )
}
