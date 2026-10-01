import { PREPARING_NEXT } from "@/lib/review/copy"

import { EmptyPanel } from "./EmptyPanel"

interface StageCheckProps {
  /** The finished stage's words; "" draws the check alone. */
  message: string
}

// The check between stages, with a moving bar while the next screen is prepared. It replaces
// the stage and stays until that screen loads. The bar's look is the Enrich bar's
// (styles/enrich.css `.enrich-progress.indeterminate`).
export function StageCheck({ message }: StageCheckProps) {
  return (
    <EmptyPanel mark title={message} className="stage-complete">
      <p>{PREPARING_NEXT}</p>
      <div className="enrich-progress indeterminate" role="progressbar" aria-label={PREPARING_NEXT}>
        <div className="enrich-progress-fill" />
      </div>
    </EmptyPanel>
  )
}
