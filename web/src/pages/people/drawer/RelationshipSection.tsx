import { DetailsSection } from "@/components/shared";
import { FACET_BY_KEY, facetText } from "@/lib/people/facets";
import type { Person, PersonDetail } from "@/types/people";

import { Pair } from "./Pair";
import type { SectionProps } from "./sections";

// The single-choice labels, each with the model's confidence in the choice.
const CHOICES = ["relationship_kind", "mode", "hierarchy", "intro_source", "seniority", "function"] as const;

interface RelationshipSectionProps extends SectionProps {
  row: Person;
  detail: PersonDetail;
}

export function RelationshipSection({ row, detail, ...section }: RelationshipSectionProps) {
  const labelled = Object.keys(detail.probabilities).length > 0;
  return (
    <DetailsSection sectionKey="relationship" title="Relationship" {...section}>
      {labelled ? (
        <dl className="kv">
          {CHOICES.filter((key) => row[key]).map((key) => {
            const facet = FACET_BY_KEY.get(key);
            if (!facet) return null;
            const percent = Math.round((detail.choice_p[key] ?? 0) * 100);
            return <Pair key={key} term={facet.label}>{facetText(facet, row[key])} <small>{percent}%</small></Pair>;
          })}
          {row.warmth === null ? null : (
            <Pair term="Warmth">{row.warmth.toFixed(1)} of 4 <small>{row.warmthBucket}</small></Pair>
          )}
        </dl>
      ) : (
        <p className="dim">No relationship labels available.</p>
      )}
    </DetailsSection>
  );
}
