import { useMutation, useQueryClient } from "@tanstack/react-query"

import { DetailsSection, Select } from "@/components/shared"
import { writeWorth } from "@/lib/api/people"
import { label } from "@/lib/people/copy"
import type { Person, PersonDetail, Worth } from "@/types/people"

import type { SectionProps } from "./sections"
import { patchRows } from "../hooks/useDecisions"
import { PEOPLE_QUERY_KEY } from "../hooks/usePeopleQuery"

const CALLS = [
  { value: "yes", label: label("worth", "yes") },
  { value: "no", label: label("worth", "no") },
]

// The worth call as a selector beside the heading, so it changes without opening the section; under it
// whose call it was and the reason, a flag that keeps the family off the upload, and any notes.
export function DecisionSection({
  row,
  detail,
  onRefresh,
  ...section
}: SectionProps & { row: Person; detail: PersonDetail | null; onRefresh: () => void }) {
  const client = useQueryClient()
  const save = useMutation({
    mutationFn: (worth: Worth) => writeWorth([row.parent_id], worth),
    onSuccess: (results) => {
      client.setQueryData<Person[]>(PEOPLE_QUERY_KEY, (current) => patchRows(current, results))
      onRefresh()
    },
  })
  const why = [row.worth_source === "human" ? "Your call" : "", detail?.worth_reason ?? ""]
    .filter(Boolean)
    .join(": ")
  const human = row.worth === "yes" || row.worth === "no" ? row.worth : ""
  return (
    <DetailsSection
      sectionKey="decision"
      title="Decision"
      badge={
        <Select
          aria-label="Edit decision"
          value={human}
          placeholder={label("worth", row.worth || "unjudged")}
          onChange={(worth) => save.mutate(worth === "yes" ? "yes" : "no")}
          disabled={save.isPending || row.in_progress}
          options={CALLS}
          className="h-7"
        />
      }
      {...section}
    >
      {save.isPending ? <p>Saving…</p> : why ? <p>{why}</p> : null}
      {save.error ? (
        <p role="alert" className="text-destructive">
          {save.error.message}
        </p>
      ) : null}
      {row.flag ? (
        <p className="dim">
          {row.worth_source === "human" && row.worth === "yes"
            ? `${label("reason", row.flag)}: uploads on your call`
            : `${label("reason", row.flag)}: kept off the upload`}
        </p>
      ) : null}
      {detail?.worth_note ? <p className="note">{detail.worth_note}</p> : null}
      {detail?.note ? <p className="note">{detail.note}</p> : null}
    </DetailsSection>
  )
}
