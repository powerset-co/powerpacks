import { useState } from "react"

import { DetailsSection } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { label } from "@/lib/people/copy"
import type { Person, PersonDetail, Worth } from "@/types/people"

import type { SectionProps } from "./sections"

const BADGE: Record<Worth, "ok" | "warn" | "muted"> = { yes: "ok", maybe: "warn", no: "muted", "": "muted" }

// The worth call as the badge; under it whose call it was and the reason, a flag that keeps the
// family off the upload, and any notes. The badge rises in when a write changes the call, not
// when the drawer opens.
export function DecisionSection({
  row,
  detail,
  ...section
}: SectionProps & { row: Person; detail: PersonDetail | null }) {
  const [opened] = useState(row.worth)
  const why = [row.worth_source === "human" ? "Your call" : "", detail?.worth_reason ?? ""]
    .filter(Boolean)
    .join(": ")
  return (
    <DetailsSection
      sectionKey="decision"
      title="Decision"
      badge={
        <Badge
          key={row.worth}
          variant={BADGE[row.worth]}
          className={row.worth === opened ? undefined : "rise-in"}
        >
          {label("worth", row.worth || "unjudged")}
        </Badge>
      }
      {...section}
    >
      {why ? <p>{why}</p> : null}
      {row.flag ? <p className="dim">{`${label("reason", row.flag)}: kept off the upload`}</p> : null}
      {detail?.worth_note ? <p className="note">{detail.worth_note}</p> : null}
      {detail?.note ? <p className="note">{detail.note}</p> : null}
    </DetailsSection>
  )
}
