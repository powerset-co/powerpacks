import { enrichPanelShown } from "@/lib/review/screen"
import type { EnrichmentJob } from "@/lib/review/sync"
import { cn } from "@/lib/utils"
import type { ReviewPage } from "@/types/review"

import { DoneStage } from "./done/DoneStage"
import { EnrichStage } from "./enrich/EnrichStage"
import { ReviewContext } from "./hooks/useReview"
import type { ReviewToast } from "./hooks/useReviewToast"
import type { Screen } from "./hooks/useScreen"
import { useScreenReview } from "./hooks/useScreenReview"
import { LinkedinStage } from "./linkedin/LinkedinStage"
import { StageCheck } from "./shared/StageCheck"
import { SynthesisPending } from "./shared/SynthesisPending"
import { WorthStage } from "./worth/WorthStage"

interface ReviewScreenProps {
  screen: Screen
  toast: ReviewToast
  reload: () => void
  open: (href: string) => void
}

// One loaded screen: the stage for the server's view, inside the `Review` the stage works
// through. The page keys it by the load, so it starts over (and fades in) each
// time. A stage transition replaces the stage with the check until the next screen loads.
export function ReviewScreen({ screen, toast, reload, open }: ReviewScreenProps) {
  const { page } = screen
  const { review, check, leaving, job } = useScreenReview({
    screen,
    panelShown: enrichPanelShown(page),
    toast,
    reload,
    open,
  })
  return (
    <main className="review-main">
      <section className={cn("stage", leaving && "leaving")} aria-live="polite">
        <ReviewContext.Provider value={review}>
          {check === null ? <Stage page={page} job={job} /> : <StageCheck message={check} />}
        </ReviewContext.Provider>
      </section>
    </main>
  )
}

function Stage({ page, job }: { page: ReviewPage; job: EnrichmentJob | null }) {
  if (page.needs_synthesis) return <SynthesisPending />
  switch (page.view) {
    case "worth":
      return <WorthStage tab={page.tab === "" ? "review" : page.tab} />
    case "enrich":
      return <EnrichStage enrichment={page.enrichment} job={job} />
    case "linkedin":
      return <LinkedinStage />
    case "done":
      return <DoneStage checked={page.progress.linkedin_done} rejected={page.progress.rejected} />
  }
}
