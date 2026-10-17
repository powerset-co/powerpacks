import { DetailsSection } from "@/components/shared"
import { eventDate } from "@/lib/people/copy"
import type { Person, PersonDetail } from "@/types/people"

import { ConfidenceSection } from "./ConfidenceSection"
import { DecisionSection } from "./DecisionSection"
import { FactsSection } from "./FactsSection"
import { RelationshipSection } from "./RelationshipSection"
import type { SectionState } from "./sections"

interface DetailSectionsProps {
  row: Person
  detail: PersonDetail
  section: SectionState
}

// The drawer's sections in page order: what we know first, then how it reads, then the call.
// A section with nothing to say is left out.
export function DetailSections({ row, detail, section }: DetailSectionsProps) {
  return (
    <>
      <FactsSection detail={detail} {...section("facts")} />
      {detail.events.length ? (
        <DetailsSection
          sectionKey="timeline"
          title="Timeline"
          count={detail.events.length}
          {...section("timeline")}
        >
          <ol className="timeline">
            {detail.events.map((event, position) => (
              <li key={position}>
                <time>{eventDate(event.date)}</time>
                <span>{event.summary}</span>
              </li>
            ))}
          </ol>
        </DetailsSection>
      ) : null}
      <RelationshipSection row={row} detail={detail} {...section("relationship")} />
      <DecisionSection row={row} detail={detail} {...section("decision")} />
      {detail.topics.length ? (
        <DetailsSection
          sectionKey="topics"
          title="Topics"
          count={detail.topics.length}
          {...section("topics")}
        >
          <ul className="plain">
            {detail.topics.map((topic) => (
              <li key={topic}>{topic}</li>
            ))}
          </ul>
        </DetailsSection>
      ) : null}
      {detail.dossier_html ? (
        <DetailsSection sectionKey="dossier" title="Dossier" {...section("dossier")}>
          <div className="dossier" dangerouslySetInnerHTML={{ __html: detail.dossier_html }} />
        </DetailsSection>
      ) : null}
      <ConfidenceSection detail={detail} {...section("confidence")} />
    </>
  )
}
