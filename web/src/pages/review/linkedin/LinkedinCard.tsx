import { useCallback, useRef, useState } from "react"
import { flushSync } from "react-dom"

import type { DecideRequest, RetargetRequest } from "@/lib/api/review"
import { TOAST } from "@/lib/review/copy"
import { must } from "@/lib/must"
import { displayName } from "@/lib/review/person"
import { cn } from "@/lib/utils"
import type { LinkedinCardPayload, LinkedinDecision, ReviewCandidate } from "@/types/review"

import { DecisionCard } from "../shared/DecisionCard"
import { PersonCard } from "../shared/PersonCard"
import { ScrollRegion } from "../shared/ScrollRegion"
import { DECISION, feedbackContext, OPTIONS_INTRO, QUESTION } from "./copy"
import { FeedbackPopover } from "./FeedbackPopover"
import { GuidanceForm } from "./GuidanceForm"
import { LinkedinOption } from "./LinkedinOption"
import { PersonMenu } from "./PersonMenu"
import { CARD, type CardPhase } from "./phase"
import { popoverPlace, type PopoverPlace } from "./place"

interface LinkedinCardProps {
  card: NonNullable<LinkedinCardPayload["card"]>
  phase: CardPhase
  /** A decision and the toast that follows it. */
  onDecide: (request: DecideRequest, message: string) => void
  /** Typed guidance for the paid re-research. */
  onRetarget: (request: RetargetRequest) => void
}

// One person and the profile (or profiles) they might be.
// The frame stays while its contents swap; a new person's contents start over.
export function LinkedinCard(props: LinkedinCardProps) {
  const { card, phase } = props
  return (
    <DecisionCard
      cardKey={card.person.slug}
      swapping={CARD[phase].fading}
      className={cn("identity-card", card.candidates.length > 1 && "identity-card-multi")}
    >
      <CardContents {...props} />
    </DecisionCard>
  )
}

function CardContents({ card, phase, onDecide, onRetarget }: LinkedinCardProps) {
  const { person, candidates } = card
  // The person's own facts, the guidance box and Skip all speak for the first candidate.
  const first = must(candidates[0], "the card's first candidate")
  const several = candidates.length > 1
  const { locked, retargetOff, queuedNote } = CARD[phase]

  const menu = useRef<HTMLDivElement>(null)
  const guidance = useRef<HTMLTextAreaElement>(null)
  const [guidanceOpen, setGuidanceOpen] = useState(false)
  /** Where the feedback popover sits; null while it is shut. */
  const [feedbackAt, setFeedbackAt] = useState<PopoverPlace | null>(null)
  const closeFeedback = useCallback(() => setFeedbackAt(null), [])

  const decide = (candidate: ReviewCandidate, decision: LinkedinDecision, message: string) =>
    onDecide({ pub: candidate.row_key, decision, parent_slug: person.slug }, message)
  const skip = () => decide(first, "detach", TOAST.skipped)

  // "No" / "None of these": the right profile is not here, so the box opens with the caret in it.
  const openGuidance = () => {
    flushSync(() => setGuidanceOpen(true))
    guidance.current?.focus({ preventScroll: true })
  }

  return (
    <>
      <PersonMenu
        anchor={menu}
        disabled={locked}
        onFeedback={() => setFeedbackAt(popoverPlace(must(menu.current, "the person menu")))}
      />
      <ScrollRegion>
        <PersonCard person={person} candidate={first} personOnly={several} />
        {several ? (
          <>
            <div className="linkedin-options-intro">{OPTIONS_INTRO}</div>
            <ul className="linkedin-options">
              {candidates.map((candidate) => (
                <LinkedinOption
                  key={candidate.row_key}
                  person={person}
                  candidate={candidate}
                  disabled={locked}
                  onUse={() => decide(candidate, "keep", TOAST.saved)}
                />
              ))}
            </ul>
          </>
        ) : null}
      </ScrollRegion>
      <div className="identity-decision">
        {several ? null : (
          <div className="question">
            {QUESTION.usual}
            {QUESTION.or}
            <button type="button" className="skip-link" disabled={locked} onClick={skip}>
              {DECISION.skip}
            </button>
            {QUESTION.after}
          </div>
        )}
        <div className="binary-actions">
          <button
            type="button"
            className="button button-outline"
            aria-expanded={guidanceOpen}
            disabled={locked}
            onClick={openGuidance}
          >
            {several ? DECISION.noneOfThese : DECISION.no}
          </button>
          {several ? (
            <button type="button" className="button button-outline" disabled={locked} onClick={skip}>
              {DECISION.skip}
            </button>
          ) : (
            <button
              type="button"
              className="button button-primary"
              disabled={locked}
              onClick={() => decide(first, "keep", TOAST.saved)}
            >
              {DECISION.use}
            </button>
          )}
        </div>
        <GuidanceForm
          open={guidanceOpen}
          onToggle={setGuidanceOpen}
          field={guidance}
          disabled={retargetOff}
          queued={queuedNote}
          onFix={(url) =>
            onDecide(
              { pub: first.row_key, decision: "fix", new_url: url, parent_slug: person.slug },
              TOAST.applied,
            )
          }
          onRetarget={(text) => onRetarget({ pub: first.row_key, parent_slug: person.slug, guidance: text })}
        />
      </div>
      {feedbackAt ? (
        <FeedbackPopover
          anchor={menu}
          place={feedbackAt}
          context={feedbackContext(displayName(person, first, several))}
          pub={first.row_key}
          slug={person.slug}
          onClose={closeFeedback}
        />
      ) : null}
    </>
  )
}
