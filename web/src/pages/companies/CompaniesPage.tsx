import { useState } from "react"

import { CompanyLogo, EmptyState, Page, Spinner } from "@/components/shared"
import { ScopePicker } from "@/components/shared/ScopePicker"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { defaultScope, useSearchScope } from "@/lib/agent/useSearchScope"
import { searchCompanies, suggestCompanies } from "@/lib/api/companies"
import { errorText } from "@/lib/api/http"
import { AutocompleteInput, type EntityValue } from "@/pages/agent/AutocompleteInput"
import type { Company, CompanyScope, CompanySearchResponse } from "@/types/companies"
import type { ChatScope } from "@/types/searches"

import { CompanyView } from "./CompanyView"
import { CardHead, PageTable, Pager } from "./parts"
import { CARD, count, location, WIDE_ONLY } from "./table"

export const PAGE_SIZE = 100

function scopeOf(scope: ChatScope): CompanyScope {
  return "set_id" in scope ? scope.set_id : "local"
}

/** A company opened from the results, with the scope and positions its list was searched with. */
interface Opened {
  company: Company
  scope: CompanyScope
  allPositions: boolean
}

/** What the shown results were searched with, so paging and a company's people ask the same. */
interface Searched {
  name: string
  scope: CompanyScope
  allPositions: boolean
  response: CompanySearchResponse
}

// The cloud app's Companies page, by name only: a search card (name, all positions, Search in),
// the matching companies a page at a time, and one company's people.
export function CompaniesPage() {
  const searchScope = useSearchScope()
  const [picked, setPicked] = useState<ChatScope | null>(null)
  const scope = picked ?? (searchScope.data ? defaultScope(searchScope.data) : null)
  const [names, setNames] = useState<EntityValue[]>([])
  const [allPositions, setAllPositions] = useState(true)
  const [searched, setSearched] = useState<Searched | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [opened, setOpened] = useState<Opened | null>(null)

  const name = names[0]?.display_value ?? ""
  const run = (request: Omit<Searched, "response">, offset: number) => {
    setSearching(true)
    setError(null)
    searchCompanies({
      scope: request.scope,
      name: request.name,
      position_type: request.allPositions ? "all" : "current",
      limit: PAGE_SIZE,
      offset,
    })
      .then((response) => setSearched({ ...request, response }))
      .catch((failure: unknown) => setError(errorText(failure)))
      .finally(() => setSearching(false))
  }

  if (opened) {
    return (
      <Page title="Companies">
        <CompanyView
          company={opened.company}
          scope={opened.scope}
          allPositions={opened.allPositions}
          onBack={() => setOpened(null)}
        />
      </Page>
    )
  }

  return (
    <Page title="Companies">
      <p className="-mt-3 m-0 text-[13px] text-muted-foreground">Search for companies by name</p>

      {/* Not a form: the suggestion list's buttons would submit it. */}
      <div className={`${CARD} flex flex-col gap-2 px-5 py-4`}>
        <Label htmlFor="company-name">Company Name</Label>
        {/* One row: the name box a third wide, then the positions checkbox, the scope and Search;
            items start-aligned so the picked-company chip under the box moves nothing. */}
        <div className="flex flex-wrap items-start gap-3">
          {/* Keyed by scope: a new scope starts the box over, with no pick or suggestions left from the old. */}
          <AutocompleteInput
            key={scope ? scopeOf(scope) : ""}
            id="company-name"
            className="w-full min-w-[240px] sm:w-1/3"
            placeholder="e.g., Stripe, OpenAI..."
            value={names}
            // One company at a time: a new pick replaces the last.
            onChange={(values) => setNames(values.slice(-1))}
            onSearch={(query) => (scope ? suggestCompanies(query, scopeOf(scope)) : Promise.resolve([]))}
            maxDisplayed={1}
          />
          <div className="flex h-10 items-center gap-2">
            <Checkbox
              id="all-positions"
              checked={allPositions}
              onCheckedChange={(checked) => setAllPositions(checked === true)}
            />
            <Label htmlFor="all-positions">Include all positions</Label>
          </div>
          <ScopePicker
            value={scope}
            onChange={(next) => {
              setPicked(next)
              setNames([])
            }}
            className="w-auto max-w-[280px] gap-2"
          />
          <Button
            variant="primary"
            size="lg"
            disabled={searching || !name || !scope}
            onClick={() => {
              if (scope) run({ name, scope: scopeOf(scope), allPositions }, 0)
            }}
          >
            {searching ? <Spinner /> : null}
            Search
          </Button>
        </div>
      </div>

      {error ? <p className="m-0 text-[13px] text-bad">{error}</p> : null}

      {/* Hidden while a search runs, so no company opens from the list being replaced. */}
      {searched && !searching ? (
        <Results
          searched={searched}
          onPage={(page) => run(searched, page * PAGE_SIZE)}
          onOpen={(company) =>
            setOpened({ company, scope: searched.scope, allPositions: searched.allPositions })
          }
        />
      ) : null}
    </Page>
  )
}

/** The matching companies, a page at a time; a row opens the company. */
function Results({
  searched,
  onPage,
  onOpen,
}: {
  searched: Searched
  onPage: (page: number) => void
  onOpen: (company: Company) => void
}) {
  const { response } = searched
  const { companies } = response
  return (
    <section className={CARD} aria-label="Results">
      <CardHead
        title={
          companies.length
            ? `Results (${response.offset + 1}–${response.offset + companies.length} of ${response.total.toLocaleString()})`
            : "Results"
        }
      />
      {companies.length ? (
        <PageTable
          label="Companies"
          columns={[
            { label: "Company Name" },
            { label: "Location", className: `text-muted-foreground ${WIDE_ONLY}` },
            { label: "Headcount", className: `text-right tabular-nums ${WIDE_ONLY}` },
            { label: "People", className: "text-right tabular-nums" },
          ]}
          grid="grid-cols-[minmax(0,1.6fr)_minmax(0,1.4fr)_96px_72px] max-[680px]:grid-cols-[minmax(0,1fr)_64px]"
          rowClassName="relative hover:bg-secondary"
          items={companies}
          getKey={(company) => company.id}
          cells={(company) => [
            // The name's button covers the row, so a click anywhere on it opens the company.
            <button
              key="name"
              type="button"
              className="flex min-w-0 cursor-pointer items-center gap-2.5 border-0 bg-transparent p-0 text-left font-semibold text-foreground after:absolute after:inset-0 hover:underline"
              onClick={() => onOpen(company)}
            >
              <CompanyLogo company={company.name} src={company.logo_url ?? undefined} size={24} />
              <span className="break-words">{company.name}</span>
            </button>,
            location(company) || "–",
            count(company.headcount),
            count(company.people_count),
          ]}
        />
      ) : (
        <EmptyState className="my-6">No companies matched {searched.name}.</EmptyState>
      )}
      <Pager
        page={Math.floor(response.offset / PAGE_SIZE)}
        pages={Math.ceil(response.total / PAGE_SIZE)}
        hasNext={response.offset + PAGE_SIZE < response.total}
        disabled={false}
        onPage={onPage}
      />
    </section>
  )
}
