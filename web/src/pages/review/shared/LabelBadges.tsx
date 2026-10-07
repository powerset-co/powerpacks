import { moreLabels } from "@/lib/review/copy"
import { foldLabels, labelTooltip } from "@/lib/review/person"
import type { ReviewCandidate } from "@/types/review"

interface LabelBadgesProps {
  /** Every label that cleared the threshold, strongest first. */
  labels: readonly string[]
  candidate?: ReviewCandidate | null
}

// A person's label badges: the first three labels, then "+N" whose tooltip (hover or keyboard
// focus) lists the rest. A judged candidate adds its confidence badge and, under the row, the
// judge's reason in plain text.
export function LabelBadges({ labels, candidate }: LabelBadgesProps) {
  const confidence = candidate?.confidence
  const reason = confidence != null ? (candidate?.reason ?? "") : ""
  if (!labels.length && confidence == null) return null
  const { shown, rest } = foldLabels(labels)
  const tooltip = labelTooltip(rest)
  return (
    <>
      <span className="person-labels">
        {confidence != null ? (
          <span className="person-label">{Math.round(confidence * 100)}% confidence</span>
        ) : null}
        {shown.map((label) => (
          <span key={label} className="person-label">
            {label}
          </span>
        ))}
        {rest.length ? (
          <span className="person-label-more" tabIndex={0} role="button" aria-label={moreLabels(tooltip)}>
            +{rest.length}
            <span className="person-label-tooltip" role="tooltip">
              {tooltip}
            </span>
          </span>
        ) : null}
      </span>
      {reason ? <p className="person-reason">{reason}</p> : null}
    </>
  )
}
