import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { CompanyLogo, EmptyState } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { errorText } from "@/lib/api/http"
import { searchCompanies } from "@/lib/api/companies"
import { LinkedInLink } from "@/pages/searches/run/LinkedInLink"
import type { Company, CompanyPerson, CompanyScope } from "@/types/companies"

import { CardHead, PageTable, Pager } from "./parts"
import { CARD, count, location, WIDE_ONLY } from "./table"

export const PEOPLE_PAGE_SIZE = 50

// The cloud page's people box: every word of the query starts a word of the name.
function normalize(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
}

function matches(person: CompanyPerson, query: string): boolean {
  const words = normalize(query).split(" ").filter(Boolean)
  const name = normalize(person.name)
  if (!words.length || name.includes(words.join(" "))) return true
  const parts = name.split(" ")
  return words.every((word) => parts.some((part) => part.startsWith(word)))
}

/** The person's LinkedIn profile; the cloud's placeholder people ("synth-…") have none. */
function profile(person: CompanyPerson): string {
  const handle = person.public_identifier
  return handle && !handle.startsWith("synth-") ? `https://www.linkedin.com/in/${handle}` : ""
}

function tenure(years: number | null): string {
  return years != null && years > 0 ? `${years}y` : "–"
}

/** One company: its header, then the people with a position there, a page at a time. */
export function CompanyView({
  company,
  scope,
  allPositions,
  onBack,
}: {
  company: Company
  scope: CompanyScope
  allPositions: boolean
  onBack: () => void
}) {
  const [page, setPage] = useState(0)
  const [query, setQuery] = useState("")
  const people_page = useQuery({
    queryKey: ["company-people", scope, company.id, allPositions, page],
    queryFn: async () => {
      const response = await searchCompanies({
        scope,
        id: company.id,
        name: company.name,
        include_people: true,
        position_type: allPositions ? "all" : "current",
        people_limit: PEOPLE_PAGE_SIZE,
        people_offset: page * PEOPLE_PAGE_SIZE,
        people_sort: "current",
        people_dir: "desc",
        limit: 10,
      })
      return response.companies.find(({ id }) => id === company.id) ?? company
    },
    retry: false,
  })
  const loaded = people_page.data ?? null
  const error = people_page.error ? errorText(people_page.error) : null

  const shown = loaded ?? company
  const people = loaded?.people ?? []
  const filtered = people.filter((person) => matches(person, query))
  const pages = Math.max(1, Math.ceil(shown.people_count / PEOPLE_PAGE_SIZE))
  const start = page * PEOPLE_PAGE_SIZE
  const where = location(shown)

  return (
    <>
      <div>
        <Button variant="ghost" onClick={onBack}>
          ← Back to results
        </Button>
      </div>

      <section className={`${CARD} flex flex-wrap items-start gap-4 px-5 py-4`}>
        <CompanyLogo company={shown.name} src={shown.logo_url ?? undefined} size={24} className="mt-1" />
        <div className="min-w-0 flex-1">
          <h2 className="m-0 break-words text-lg font-semibold">{shown.name}</h2>
          {where ? <p className="m-0 mt-1 text-[13px] text-muted-foreground">{where}</p> : null}
        </div>
        <div className="text-[13px] text-muted-foreground sm:text-right">
          <div>Headcount: {count(shown.headcount)}</div>
          <div>People: {count(shown.people_count)}</div>
        </div>
      </section>

      <section className={CARD} aria-label="People">
        <CardHead title={`People (${count(shown.people_count)})`}>
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search people by name..."
            aria-label="Search people by name"
            className="h-8 w-full sm:w-[320px]"
          />
        </CardHead>
        {error ? (
          <p className="m-0 px-5 py-4 text-[13px] text-bad">{error}</p>
        ) : !loaded ? (
          <EmptyState className="my-6">Loading people…</EmptyState>
        ) : !people.length ? (
          <EmptyState className="my-6">No people found for this company.</EmptyState>
        ) : !filtered.length ? (
          <EmptyState className="my-6">No people matched that name search.</EmptyState>
        ) : (
          <>
            <p className="m-0 px-5 pt-3 text-xs text-muted-foreground">
              Showing {start + 1}–{start + people.length} of {shown.people_count.toLocaleString()}
              {filtered.length !== people.length ? ` (${filtered.length} matches on this page)` : ""}
            </p>
            <PageTable
              label="People"
              columns={[
                { label: "Name", className: "font-medium" },
                { label: "Position", className: "break-words" },
                { label: "Tenure", className: `tabular-nums text-muted-foreground ${WIDE_ONLY}` },
                { label: "Current/Past" },
              ]}
              grid="grid-cols-[minmax(0,1.2fr)_minmax(0,1.6fr)_64px_108px] max-[680px]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_96px]"
              items={filtered}
              getKey={(person) => `${person.id}:${person.position_title}`}
              cells={(person) => [
                <span key="name" className="break-words">
                  <LinkedInLink row={{ name: person.name, linkedin_url: profile(person) }} />
                  {person.name}
                </span>,
                person.position_title || "—",
                tenure(person.tenure_years),
                <Badge key="status" variant={person.is_current ? "ok" : "muted"}>
                  {person.is_current ? "Current" : "Past"}
                </Badge>,
              ]}
            />
          </>
        )}
        <Pager
          page={page}
          pages={pages}
          hasNext={shown.people_has_more ?? start + PEOPLE_PAGE_SIZE < shown.people_count}
          disabled={!loaded}
          onPage={setPage}
        />
      </section>
    </>
  )
}
