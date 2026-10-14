import { useState } from "react"

import { DetailsSection } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { label } from "@/lib/people/copy"
import type { Decision, Person, PersonDetail } from "@/types/people"

import type { SectionProps } from "./sections"

const BADGE: Record<Decision, "ok" | "muted"> = { yes: "ok", no: "muted" }

function worthLine(row: Person): string {
  const yours = row.worth_source === "human"
  // A worth reason already names worth in the line above: then only whose call it was.
  if (row.reason.startsWith("worth_")) return yours ? "Your call" : ""
  const worth = `Worth: ${label("worth", row.worth || "unjudged").toLowerCase()}`
  return yours ? `${worth} · your call` : worth
}

// Why this person is where they are: the reason, the worth call, and any notes. The badge
// rises in when a write changes the decision, not when the drawer opens on them.
export function DecisionSection({
  row,
  detail,
  ...section
}: SectionProps & { row: Person; detail: PersonDetail | null }) {
  const [opened] = useState(row.share)
  const why = [worthLine(row), detail?.worth_reason ?? ""].filter(Boolean).join(": ")
  return (
    <DetailsSection
      sectionKey="decision"
      title="Decision"
      badge={
        <Badge
          key={row.share}
          variant={BADGE[row.share]}
          className={row.share === opened ? undefined : "rise-in"}
        >
          {label("share", row.share)}
        </Badge>
      }
      {...section}
    >
      <p>{label("reason", row.reason)}</p>
      {why ? <p className="dim">{why}</p> : null}
      {detail?.worth_note ? <p className="note">{detail.worth_note}</p> : null}
      {detail?.note ? <p className="note">{detail.note}</p> : null}
    </DetailsSection>
  )
}
