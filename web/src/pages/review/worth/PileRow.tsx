import { useState } from "react"

import { FACT, SCROLL_DOWN, UNNAMED } from "@/lib/review/copy"
import { cn } from "@/lib/utils"
import type { DecisionRow } from "@/types/review"

import { useScrollCue } from "../hooks/useScrollCue"
import { Dossier } from "../shared/Dossier"
import { LabelBadges } from "../shared/LabelBadges"
import { ReviewAvatar } from "../shared/ReviewAvatar"
import { ANSWER, flipLabel, WHO_THEY_ARE, whyHeading } from "./copy"
import { otherPile, type Pile } from "./piles"

interface PileRowProps {
  row: DecisionRow
  /** The pile the row sits in. */
  pile: Pile
  /** Its flip is saving: the row is fading and its button is off. */
  leaving: boolean
  /** Move the person to the other pile. */
  onFlip: () => void
}

// templates/decision_row.html.j2: a collapsed row (caret, avatar, name, labels, the flip
// button) that opens to the person's contact, why they are in this pile, and their dossier.
// The dossier is asked for the first time the row opens and kept from then on.
export function PileRow({ row, pile, leaving, onFlip }: PileRowProps) {
  const { person, candidate, reason } = row
  const [opened, setOpened] = useState(false)
  const { scroller, more, scrollDown, refresh } = useScrollCue<HTMLDivElement>()
  const name = person.name || UNNAMED
  const to = otherPile(pile)
  return (
    <details
      className={cn("decision-row", leaving && "leaving")}
      onToggle={(event) => {
        if (event.currentTarget.open) setOpened(true)
        refresh()
      }}
    >
      <summary className="decision-row-summary">
        <span className="decision-row-caret" aria-hidden="true" />
        <ReviewAvatar person={person} candidate={candidate} />
        <div className="decision-row-main">
          <div className="person-name-line">
            <strong>{name}</strong>
            <LabelBadges labels={person.labels} />
          </div>
        </div>
        <div className="decision-row-actions">
          <button
            className="button button-ghost"
            type="button"
            aria-label={flipLabel(name, to)}
            disabled={leaving}
            onClick={(event) => {
              // The button sits in the summary: without this the click also opens the row.
              event.preventDefault()
              onFlip()
            }}
          >
            {ANSWER[to]}
          </button>
        </div>
      </summary>
      <div className="decision-row-detail" ref={scroller}>
        <div className="decision-expanded-profile">
          <h2>{name}</h2>
          <LabelBadges labels={person.labels} />
        </div>
        <dl className="row-facts">
          {candidate?.contacts ? (
            <div>
              <dt>{FACT.contact}</dt>
              <dd>{candidate.contacts}</dd>
            </div>
          ) : null}
          <div>
            <dt>{whyHeading(pile)}</dt>
            <dd>{reason}</dd>
          </div>
        </dl>
        <h4 className="dossier-heading">{WHO_THEY_ARE}</h4>
        {opened ? <Dossier slug={person.slug} className="row-facts" /> : null}
        <button
          className="scroll-cue"
          type="button"
          aria-label={SCROLL_DOWN}
          hidden={!more}
          onClick={scrollDown}
        >
          ⌄
        </button>
      </div>
    </details>
  )
}
