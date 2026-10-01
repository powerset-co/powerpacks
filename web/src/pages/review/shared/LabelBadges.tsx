import { moreLabels } from "@/lib/review/copy"
import { foldLabels, labelTooltip } from "@/lib/review/person"

interface LabelBadgesProps {
  /** Every label that cleared the threshold, strongest first. */
  labels: readonly string[]
}

// A person's label badges: the first three labels, then "+N" whose tooltip (hover or keyboard
// focus) lists the rest.
export function LabelBadges({ labels }: LabelBadgesProps) {
  if (!labels.length) return null
  const { shown, rest } = foldLabels(labels)
  const tooltip = labelTooltip(rest)
  return (
    <span className="person-labels">
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
  )
}
