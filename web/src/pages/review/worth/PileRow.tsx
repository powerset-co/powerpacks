import { DOSSIER, SCROLL_DOWN, UNNAMED } from "@/lib/review/copy"
import { cn } from "@/lib/utils"
import type { DecisionRow } from "@/types/review"

import { useScrollCue } from "../hooks/useScrollCue"
import { LabelBadges } from "../shared/LabelBadges"
import { PersonCard } from "../shared/PersonCard"
import { ReviewAvatar } from "../shared/ReviewAvatar"
import { ANSWER, flipLabel, WHO_THEY_ARE, whyHeading } from "./copy"
import { otherPile, type Pile } from "./piles"
import type { RowDetails } from "./useOpenedRows"

interface PileRowProps {
  row: DecisionRow
  /** The pile the row sits in. */
  pile: Pile
  /** The row's place in the pile; the list measures the row by it. */
  index: number
  /** The list's measuring ref: an opened row is taller, and the rows below it move. */
  measure: (row: HTMLDetailsElement | null) => void
  open: boolean
  /** What the row read when it first opened; undefined until then. */
  details: RowDetails | undefined
  /** Its flip is saving: the row is fading and its button is off. */
  leaving: boolean
  onToggle: (open: boolean) => void
  /** Move the person to the other pile. */
  onFlip: () => void
}

// A collapsed row (caret, initials, name, labels, the flip button) that opens to why the
// person is in this pile and, once read, who they are. A pile's row carries no profile, so
// its avatar is the person's initials.
export function PileRow(props: PileRowProps) {
  const { row, pile, index, measure, open, details, leaving, onToggle, onFlip } = props
  const { person, reason } = row
  const { scroller, more, scrollDown, refresh } = useScrollCue<HTMLDivElement>()
  const name = person.name || UNNAMED
  const to = otherPile(pile)
  return (
    <details
      ref={measure}
      data-index={index}
      className={cn("decision-row", leaving && "leaving")}
      open={open}
      onToggle={(event) => {
        onToggle(event.currentTarget.open)
        refresh()
      }}
    >
      <summary className="decision-row-summary">
        <span className="decision-row-caret" aria-hidden="true" />
        <ReviewAvatar person={person} candidate={null} />
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
        <dl className="row-facts">
          <div>
            <dt>{whyHeading(pile)}</dt>
            <dd>{reason}</dd>
          </div>
        </dl>
        <Details details={details} />
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

// An opened row's details: "Loading…" until they are read, then the person's profile card,
// "Who they are" and the dossier; "No details found" when the server has none, "Could not load
// details" when the read fails.
function Details({ details }: Pick<PileRowProps, "details">) {
  if (details?.status !== "ready") {
    const waiting = details === undefined || details.status === "loading"
    return (
      <div className="dossier-text" aria-busy={waiting || undefined}>
        {details ? DOSSIER[details.status] : null}
      </div>
    )
  }
  return (
    <div className="dossier-text">
      <PersonCard person={details.person} candidate={details.candidate} dossier={false} />
      <h4 className="dossier-heading">{WHO_THEY_ARE}</h4>
      {/* The HTML is this machine's own server rendering the person's own markdown file. */}
      <div className="row-facts" dangerouslySetInnerHTML={{ __html: details.dossier }} />
    </div>
  )
}
