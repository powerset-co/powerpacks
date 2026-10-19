import { useMutation, useQueryClient } from "@tanstack/react-query"
import { DetailsSection, Select } from "@/components/shared"
import { writeRelationship } from "@/lib/api/people"
import { facetOf, facetText } from "@/lib/people/facets"
import type { Person, PersonDetail } from "@/types/people"

import { Pair } from "./Pair"
import type { SectionProps } from "./sections"
import { PEOPLE_QUERY_KEY } from "../hooks/usePeopleQuery"

// The single-choice labels, each with the model's confidence in the choice; N/A carries none.
const CHOICES = ["intro_source", "function"] as const
const RELATIONSHIPS = [
  "family",
  "romantic_partner",
  "close_friend",
  "friend",
  "acquaintance",
  "colleague",
  "business_contact",
  "service_provider",
  "community",
  "stranger",
  "unknown",
]

interface RelationshipSectionProps extends SectionProps {
  row: Person
  detail: PersonDetail
  onRefresh: () => void
}

export function RelationshipSection({ row, detail, onRefresh, ...section }: RelationshipSectionProps) {
  const client = useQueryClient()
  const save = useMutation({
    mutationFn: (kind: string) => writeRelationship(row.parent_id, kind),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: PEOPLE_QUERY_KEY })
      onRefresh()
    },
  })
  const labelled = Object.keys(detail.probabilities).length > 0
  return (
    <DetailsSection sectionKey="relationship" title="Relationship" {...section}>
      <dl className="kv">
        <Pair term="Relationship">
          <Select
            aria-label="Edit relationship"
            value={row.relationship_kind || "unknown"}
            onChange={(kind) => save.mutate(kind)}
            disabled={save.isPending}
            options={RELATIONSHIPS.map((value) => ({
              value,
              label: facetText(facetOf("relationship_kind"), value),
            }))}
          />
          {save.isPending ? (
            <small>Saving…</small>
          ) : detail.relationship_kind_source === "human" ? (
            <small>Your choice · kept on refresh</small>
          ) : detail.choice_p.relationship_kind !== undefined ? (
            <small>{Math.round(detail.choice_p.relationship_kind * 100)}%</small>
          ) : null}
        </Pair>
      </dl>
      {save.error ? (
        <p role="alert" className="text-destructive">
          {save.error.message}
        </p>
      ) : null}
      {labelled ? (
        <dl className="kv">
          {CHOICES.filter((key) => row[key]).map((key) => {
            const facet = facetOf(key)
            const percent = Math.round((detail.choice_p[key] ?? 0) * 100)
            return (
              <Pair key={key} term={facet.label}>
                {facetText(facet, row[key])} {row[key] === "unknown" ? null : <small>{percent}%</small>}
              </Pair>
            )
          })}
          {row.warmth === null ? null : (
            <Pair term="Warmth">
              {row.warmth.toFixed(1)} of 4 <small>{row.warmthBucket}</small>
            </Pair>
          )}
        </dl>
      ) : null}
    </DetailsSection>
  )
}
