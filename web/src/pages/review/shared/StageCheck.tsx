import { EmptyPanel } from "./EmptyPanel"

interface StageCheckProps {
  /** The finished stage's words; "" draws the check alone. */
  message: string
}

// reconcile_review.js `leaveAndNavigate`: the check between stages. It replaces the stage and
// stays until the next screen loads.
export function StageCheck({ message }: StageCheckProps) {
  return <EmptyPanel mark title={message} className="stage-complete" />
}
