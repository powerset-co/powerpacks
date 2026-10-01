import type { ReactNode } from "react"

import { FACT, VIEW_LINKEDIN } from "@/lib/review/copy"
import { displayName, summaryOf } from "@/lib/review/person"
import { cn } from "@/lib/utils"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

import { FactList } from "../shared/FactList"
import { ReviewAvatar } from "../shared/ReviewAvatar"
import { DECISION, OPTION } from "./copy"

/** Which of the three things an option is. */
type OptionKind =
  /** A researched profile: no LinkedIn was confirmed. */
  | "researched"
  /** A fetched LinkedIn profile. */
  | "linked"
  /** A LinkedIn candidate whose profile has not been fetched. */
  | "unfetched"

function optionKind(candidate: ReviewCandidate): OptionKind {
  if (candidate.synthetic) return "researched"
  if (candidate.url) return "linked"
  return "unfetched"
}

interface LinkedinOptionProps {
  person: ReviewPerson
  candidate: ReviewCandidate
  /** A decision is saving. */
  disabled: boolean
  /** "Use this profile" was pressed. */
  onUse: () => void
}

// The `option` macro: one of several profiles a person might be. A LinkedIn option is
// identity evidence only (headline, Work, Education); the contact and the dossier belong to
// the person above. A researched option shows its research summary in place of a headline.
export function LinkedinOption({ person, candidate, disabled, onUse }: LinkedinOptionProps) {
  const kind = optionKind(candidate)
  const headline = kind === "researched" ? "" : summaryOf(candidate)
  return (
    <li className={cn("linkedin-option", kind === "researched" ? "option-synthetic" : "option-linkedin")}>
      <div className="profile-card">
        <ReviewAvatar person={person} candidate={candidate} />
        <div className="profile-copy">
          <h3>{displayName(person, candidate, false)}</h3>
          <KindLine kind={kind} url={candidate.url} />
          {headline ? <p>{headline}</p> : null}
        </div>
      </div>
      <section className="details">
        <dl>
          {candidate.experiences.length ? (
            <Fact label={FACT.work}>
              <FactList items={candidate.experiences} />
            </Fact>
          ) : null}
          {candidate.education.length ? (
            <Fact label={FACT.education}>
              <FactList items={candidate.education} />
            </Fact>
          ) : null}
          {kind === "researched" && candidate.headline ? (
            <Fact label={OPTION.researchSummary}>{candidate.headline}</Fact>
          ) : null}
        </dl>
      </section>
      <div className="binary-actions">
        <button className="button button-primary" type="button" disabled={disabled} onClick={onUse}>
          {DECISION.use}
        </button>
      </div>
    </li>
  )
}

function KindLine({ kind, url }: { kind: OptionKind; url: string }) {
  switch (kind) {
    case "researched":
      return <span className="option-kind">{OPTION.researched}</span>
    case "linked":
      return (
        <a className="linkedin-label" href={url} target="_blank" rel="noreferrer">
          {VIEW_LINKEDIN}
          <span aria-hidden="true">↗</span>
        </a>
      )
    case "unfetched":
      return <span className="option-kind option-empty">{OPTION.unfetched}</span>
  }
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}
