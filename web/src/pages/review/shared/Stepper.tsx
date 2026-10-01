import { Link } from "react-router-dom"

import { STEPPER_LABEL } from "@/lib/review/copy"
import { stepHref } from "@/lib/review/links"
import { ACTIVE_STEP, stepCount, stepMarker, stepState } from "@/lib/review/steps"
import { cn } from "@/lib/utils"
import type { ReviewStep, ReviewView } from "@/types/review"

interface StepperProps {
  /** The three steps with their live counts. */
  steps: readonly ReviewStep[]
  /** The screen on show: its step is highlighted (Done lights the last). */
  view: ReviewView
}

// templates/step.html.j2: three linked steps with a line between. A step opens its stage as a
// deliberately opened screen (`preview=1`).
export function Stepper({ steps, view }: StepperProps) {
  return (
    <nav className="stepper" aria-label={STEPPER_LABEL}>
      {steps.map((step, position) => {
        const state = stepState(step, position === ACTIVE_STEP[view])
        const count = stepCount(step.count)
        return [
          position > 0 ? <i key={`line-${step.number}`} className="step-line" /> : null,
          <Link
            key={step.number}
            to={stepHref(step.stage)}
            className={cn("step", state !== "idle" && state)}
            data-step={step.stage}
          >
            <span>{stepMarker(step)}</span>
            <div>
              {step.label}
              {count ? <small>{count}</small> : null}
            </div>
          </Link>,
        ]
      })}
    </nav>
  )
}
