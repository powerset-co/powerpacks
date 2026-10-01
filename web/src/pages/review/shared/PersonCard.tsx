import type { ReactNode } from "react"

import { FACT, VIEW_LINKEDIN } from "@/lib/review/copy"
import { displayName, profileUrl, summaryOf } from "@/lib/review/person"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

import { Dossier } from "./Dossier"
import { FactList } from "./FactList"
import { LabelBadges } from "./LabelBadges"
import { ReviewAvatar } from "./ReviewAvatar"
import { SourceBadges } from "./SourceBadges"

interface PersonCardProps {
  person: ReviewPerson
  /** The profile shown with the person; null when the parent has none. */
  candidate: ReviewCandidate | null
  /** The person alone (a card with several candidates): the parent's name, the contact and the
   *  dossier; the LinkedIn link and facts belong to each option. */
  personOnly?: boolean
  /** Load the dossier under the facts. A holder that draws it under its own heading (an
   *  opened decision row) turns it off. */
  dossier?: boolean
}

// The `profile` macro: avatar, source badges, name, label badges and "View LinkedIn", then the
// facts (Contact, Summary, Location, Work, Education) and the person's dossier.
export function PersonCard({ person, candidate, personOnly = false, dossier = true }: PersonCardProps) {
  const url = profileUrl(candidate, personOnly)
  const facts = personOnly ? null : candidate
  const summary = facts ? summaryOf(facts) : ""
  return (
    <>
      <div className="profile-card">
        <ReviewAvatar person={person} candidate={candidate} />
        <div className="profile-copy">
          {person.sources.length ? (
            <div className="eyebrow-row">
              <SourceBadges sources={person.sources} />
            </div>
          ) : null}
          <h2>{displayName(person, candidate, personOnly)}</h2>
          <LabelBadges labels={person.labels} />
          {url ? (
            <a className="linkedin-label" href={url} target="_blank" rel="noreferrer">
              {VIEW_LINKEDIN}
              <span aria-hidden="true">↗</span>
            </a>
          ) : null}
        </div>
      </div>
      <section className="details">
        <dl>
          {candidate?.contacts ? <Fact label={FACT.contact}>{candidate.contacts}</Fact> : null}
          {summary ? <Fact label={FACT.summary}>{summary}</Fact> : null}
          {facts?.location ? <Fact label={FACT.location}>{facts.location}</Fact> : null}
          {facts?.experiences.length ? (
            <Fact label={FACT.work}>
              <FactList items={facts.experiences} />
            </Fact>
          ) : null}
          {facts?.education.length ? (
            <Fact label={FACT.education}>
              <FactList items={facts.education} />
            </Fact>
          ) : null}
        </dl>
        {dossier ? <Dossier slug={person.slug} /> : null}
      </section>
    </>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}
