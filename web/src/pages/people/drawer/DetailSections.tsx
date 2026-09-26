import { DetailsSection } from "@/components/shared";
import { eventDate } from "@/lib/people/copy";
import type { Person, PersonDetail } from "@/types/people";

import { ConfidenceSection } from "./ConfidenceSection";
import { ContactSection } from "./ContactSection";
import { FactsSection } from "./FactsSection";
import { RelationshipSection } from "./RelationshipSection";
import type { SectionState } from "./sections";

interface DetailSectionsProps {
  row: Person;
  detail: PersonDetail;
  section: SectionState;
}

// Everything after the decision, in page order; a section with nothing to say is left out.
export function DetailSections({ row, detail, section }: DetailSectionsProps) {
  return (
    <>
      {detail.events.length ? (
        <DetailsSection sectionKey="timeline" title="Timeline" count={detail.events.length} {...section("timeline")}>
          <ol className="timeline">
            {detail.events.map((event, position) => (
              <li key={position}><time>{eventDate(event.date)}</time><span>{event.summary}</span></li>
            ))}
          </ol>
        </DetailsSection>
      ) : null}
      <FactsSection detail={detail} {...section("facts")} />
      <RelationshipSection row={row} detail={detail} {...section("relationship")} />
      {detail.topics.length ? (
        <DetailsSection sectionKey="topics" title="Topics" count={detail.topics.length} {...section("topics")}>
          <ul className="plain">{detail.topics.map((topic) => <li key={topic}>{topic}</li>)}</ul>
        </DetailsSection>
      ) : null}
      {detail.dossier_html ? (
        <DetailsSection sectionKey="dossier" title="Dossier" {...section("dossier")}>
          <div className="dossier" dangerouslySetInnerHTML={{ __html: detail.dossier_html }} />
        </DetailsSection>
      ) : null}
      <ContactSection row={row} detail={detail} {...section("contact")} />
      <ConfidenceSection detail={detail} {...section("confidence")} />
    </>
  );
}
