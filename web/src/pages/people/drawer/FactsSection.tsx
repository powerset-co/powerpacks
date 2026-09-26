import { DetailsSection } from "@/components/shared";
import { sentence } from "@/lib/people/copy";
import type { PersonDetail } from "@/types/people";

import { Lines, Pair } from "./Pair";
import type { SectionProps } from "./sections";

// What the dossier established: how they relate to the owner, work, school, names, shared context.
export function FactsSection({ detail, ...section }: SectionProps & { detail: PersonDetail }) {
  const pairs = [
    detail.employers.length ? <Pair key="employers" term="Employers"><Lines values={detail.employers} /></Pair> : null,
    detail.school ? <Pair key="school" term="School">{detail.school}</Pair> : null,
    detail.location ? <Pair key="location" term="Location">{detail.location}</Pair> : null,
    detail.aliases.length ? <Pair key="aliases" term="Also known as">{detail.aliases.join(", ")}</Pair> : null,
    detail.shared_context.length
      ? <Pair key="shared" term="Shared context"><Lines values={detail.shared_context.map(sentence)} /></Pair>
      : null,
  ].filter(Boolean);
  if (!detail.relationship_to_owner && !pairs.length) return null;
  return (
    <DetailsSection sectionKey="facts" title="Facts" {...section}>
      {detail.relationship_to_owner ? <p>{detail.relationship_to_owner}</p> : null}
      {pairs.length ? <dl className="kv">{pairs}</dl> : null}
    </DetailsSection>
  );
}
