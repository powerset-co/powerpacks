import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { scoreBand, type ResultRow } from "@/lib/searches/ranking"

const ABOUT_CLAMP = 200
const TRAIT_VARIANT = { high: "ok", medium: "warn", low: "bad" } as const

interface EvidenceProps {
  result: ResultRow
  ranked: boolean
}

// rendering.py _person_details: why they match, trait scores with reasons, the pin judge,
// what they matched on, location and about. The drawer's first sections.
export function Evidence({ result, ranked }: EvidenceProps) {
  const { row, candidate } = result
  const [aboutOpen, setAboutOpen] = useState(false)
  const pin = candidate?.pin_judgment
  const locationMatched = row.vertical_sources.includes("location")
  const clamp = row.summary.length > ABOUT_CLAMP
  return (
    <div className="review-sections" data-evidence>
      {ranked ? (
        <section>
          <h4>Overall</h4>
          <p>{result.reason}</p>
        </section>
      ) : null}
      {row.reasoning ? (
        <section>
          <h4>Why they match</h4>
          <p>{row.reasoning}</p>
        </section>
      ) : null}
      {row.traits.length ? (
        <section>
          <h4>Trait scores</h4>
          <ul className="traits" data-traits>
            {row.traits.map((trait) => (
              <li key={trait.name}>
                <Badge variant={TRAIT_VARIANT[scoreBand(trait.score)]}>
                  {Math.round(trait.score * 100)}%
                </Badge>
                <p>
                  <b>{trait.name}:</b> {trait.reason || "No evidence reason recorded."}
                </p>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {pin ? (
        <section>
          <h4>Pin confidence</h4>
          <p>
            {candidate.pin_confidence === null ? "Unscored" : `${candidate.pin_confidence}/100`} ·{" "}
            {pin.decision ?? "no decision"}. {pin.reason}
          </p>
        </section>
      ) : null}
      {row.vertical_sources.length ? (
        <section>
          <h4>Matched on</h4>
          <p className="evidence-chips">
            {row.vertical_sources.map((source) => (
              <Badge key={source} variant="muted">
                {source.charAt(0).toUpperCase() + source.slice(1)}
              </Badge>
            ))}
          </p>
        </section>
      ) : null}
      {row.profile_location && locationMatched ? (
        <section>
          <h4>Location</h4>
          <p>{row.profile_location}</p>
        </section>
      ) : null}
      {row.summary ? (
        <section>
          <h4>About</h4>
          <p className={clamp && !aboutOpen ? "evidence-clamped" : undefined}>{row.summary}</p>
          {clamp ? (
            <button type="button" className="evidence-more" onClick={() => setAboutOpen(!aboutOpen)}>
              {aboutOpen ? "Show less" : "Show more"}
            </button>
          ) : null}
        </section>
      ) : null}
    </div>
  )
}
