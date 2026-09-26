import { DetailsSection } from "@/components/shared";
import { label } from "@/lib/people/copy";
import type { PersonDetail } from "@/types/people";

import type { SectionProps } from "./sections";

// A label counts as held from this probability up; its bar turns green.
const ACTIVE_AT = 0.6;

export function ConfidenceSection({ detail, ...section }: SectionProps & { detail: PersonDetail }) {
  const probabilities = Object.entries(detail.probabilities).sort((a, b) => b[1] - a[1]);
  if (!probabilities.length) return null;
  return (
    <DetailsSection sectionKey="confidence" title="Label confidence" count={probabilities.length} {...section}>
      <div className="bars">
        {probabilities.map(([name, p]) => (
          <div key={name} className={p >= ACTIVE_AT ? "barrow active" : "barrow"}>
            <span className="name">{label("labels", name)}</span>
            <span className="track"><span className="fill" style={{ transform: `scaleX(${p.toFixed(3)})` }} /></span>
            <span className="p">{Math.round(p * 100)}%</span>
          </div>
        ))}
      </div>
    </DetailsSection>
  );
}
