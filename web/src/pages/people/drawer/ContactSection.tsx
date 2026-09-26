import { DetailsSection } from "@/components/shared";
import { formatDate, label } from "@/lib/people/copy";
import type { Person, PersonDetail } from "@/types/people";

import { Lines, Pair } from "./Pair";
import type { SectionProps } from "./sections";

function Frequency({ row }: { row: Person }) {
  if (!row.cadence) return null;
  return (
    <>
      {label("cadence", row.cadence)}
      {row.direction ? <> <small>writes: {label("direction", row.direction).toLowerCase()}</small></> : null}
    </>
  );
}

export function ContactSection({ row, detail, ...section }: SectionProps & { row: Person; detail: PersonDetail }) {
  const points = [...detail.emails, ...detail.phones];
  return (
    <DetailsSection sectionKey="contact" title="Contact" {...section}>
      <dl className="kv">
        <Pair term="Interactions"><span className="num">{row.interactions.toLocaleString()}</span></Pair>
        <Pair term="Last contact">{row.last_interaction ? formatDate(row.last_interaction) : null}</Pair>
        <Pair term="Contact frequency">{row.cadence ? <Frequency row={row} /> : null}</Pair>
        <Pair term="Email and phone">{points.length ? <Lines values={points} /> : null}</Pair>
      </dl>
    </DetailsSection>
  );
}
