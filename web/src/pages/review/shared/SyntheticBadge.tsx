import { SYNTHETIC } from "@/lib/review/copy"

// The "Synthetic" badge beside a researched profile's name. Hover or keyboard focus shows what a
// synthetic profile is; the badge is the one mark, so the header keeps its shape.
export function SyntheticBadge() {
  return (
    <span className="synthetic-badge" tabIndex={0} role="button" aria-label={SYNTHETIC.explain}>
      {SYNTHETIC.badge}
      <span className="synthetic-tooltip" role="tooltip">
        {SYNTHETIC.explain}
      </span>
    </span>
  )
}
