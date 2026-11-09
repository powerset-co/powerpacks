import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { CompanyLogo, EmptyState, Page, SearchField } from "@/components/shared"
import { ScopePicker } from "@/components/shared/ScopePicker"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { defaultScope, useSearchScope } from "@/lib/agent/useSearchScope"
import { searchCompanies } from "@/lib/api/companies"
import { errorText } from "@/lib/api/http"
import { must } from "@/lib/must"
import type { Company, CompanyScope, CompanySearchResponse } from "@/types/companies"
import type { ChatScope } from "@/types/searches"

import { CompanyView } from "./CompanyView"
import { PageTable, Pager } from "./parts"
import { CARD, count, location, WIDE_ONLY } from "./table"

export const PAGE_SIZE = 100
// Typing settles this long before the list asks again.
const TYPING_MS = 200

function scopeOf(scope: ChatScope): CompanyScope {
  return "set_id" in scope ? scope.set_id : "local"
}

/** A company opened from the list, with the scope and positions the list was loaded with. */
interface Opened {
  company: Company
  scope: CompanyScope
  allPositions: boolean
}

// Laid out as the People page is: a search line (the box filters as you type, all positions,
// Search in) over the list, which loads on open with your companies, most people first.
export function CompaniesPage() {
  const searchScope = useSearchScope()
  const [picked, setPicked] = useState<ChatScope | null>(null)
  const scope = picked ?? (searchScope.data ? defaultScope(searchScope.data) : null)
  const [text, setText] = useState("")
  const [name, setName] = useState("")
  const [allPositions, setAllPositions] = useState(true)
  const [page, setPage] = useState(0)
  const [opened, setOpened] = useState<Opened | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => {
      setName(text.trim())
      setPage(0)
    }, TYPING_MS)
    return () => clearTimeout(timer)
  }, [text])

  const list = useQuery({
    queryKey: ["companies", scope && scopeOf(scope), name, allPositions, page],
    queryFn: () =>
      searchCompanies({
        scope: scopeOf(must(scope)),
        name,
        position_type: allPositions ? "all" : "current",
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    enabled: scope !== null,
    placeholderData: keepPreviousData,
  })

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
      <div className="flex flex-wrap items-center gap-2">
        <SearchField
          className="w-full sm:w-[300px]"
          placeholder="Search companies by name or domain"
          aria-label="Search companies"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <Label className="flex h-8 cursor-pointer items-center gap-2 px-1 text-[13px] font-normal">
          <Checkbox
            checked={allPositions}
            onCheckedChange={(checked) => {
              setAllPositions(checked === true)
              setPage(0)
            }}
          />
          Include all positions
        </Label>
        <ScopePicker
          value={scope}
          onChange={(next) => {
            setPicked(next)
            setPage(0)
          }}
          className="h-8 w-auto max-w-[280px] gap-2 rounded-sm border-line-strong bg-background pl-2.5 pr-2 text-[13px] text-foreground"
        />
        {list.data ? (
          <span className="ml-auto text-[13px] text-muted-foreground">
            {list.data.total.toLocaleString()} {list.data.total === 1 ? "company" : "companies"}
          </span>
        ) : null}
      </div>

      {list.error ? <p className="m-0 text-[13px] text-bad">{errorText(list.error)}</p> : null}

      {list.data ? (
        <Results
          name={name}
          response={list.data}
          // A list still loading for a new scope or name is not opened from.
          stale={list.isPlaceholderData}
          onPage={setPage}
          onOpen={(company) => setOpened({ company, scope: scopeOf(must(scope)), allPositions })}
        />
      ) : null}
    </Page>
  )
}

/** The matching companies, a page at a time; a row opens the company. */
function Results({
  name,
  response,
  stale,
  onPage,
  onOpen,
}: {
  name: string
  response: CompanySearchResponse
  stale: boolean
  onPage: (page: number) => void
  onOpen: (company: Company) => void
}) {
  const { companies } = response
  return (
    <section className={`${CARD} ${stale ? "pointer-events-none opacity-60" : ""}`} aria-label="Companies">
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
        <EmptyState className="my-6">
          {name ? `No companies matched ${name}.` : "No companies yet."}
        </EmptyState>
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
