import { DetailsSection } from "@/components/shared";
import { Badge } from "@/components/ui/badge";
import { label } from "@/lib/people/copy";
import type { Decision, Person, PersonDetail } from "@/types/people";

import type { SectionProps } from "./sections";

const BADGE: Record<Decision, "ok" | "warn" | "muted"> = { yes: "ok", confirm: "warn", no: "muted" };

function worthLine(row: Person): string {
  const worth = `Worth: ${label("worth", row.worth || "unjudged").toLowerCase()}`;
  if (!row.worth_source) return worth;
  const by = row.worth_source === "machine" ? "AI" : "you";
  return `${worth} · Decided by ${by}`;
}

// Why this person is where they are: the reason, the worth call, and any notes.
export function DecisionSection({ row, detail, ...section }: SectionProps & { row: Person; detail: PersonDetail | null }) {
  return (
    <DetailsSection sectionKey="decision" title="Decision" badge={<Badge variant={BADGE[row.share]}>{label("share", row.share)}</Badge>} {...section}>
      <p>{label("reason", row.reason)}</p>
      <p className="dim">{worthLine(row)}{detail?.worth_reason ? `: ${detail.worth_reason}` : ""}</p>
      {detail?.worth_note ? <p className="note">{detail.worth_note}</p> : null}
      {detail?.note ? <p className="note">{detail.note}</p> : null}
    </DetailsSection>
  );
}
