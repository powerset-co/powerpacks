import { PageHeader, SearchField, Select } from "@/components/shared"
import { Skeleton } from "@/components/ui/skeleton"
import { SEARCH_LABEL, SEARCH_PLACEHOLDER } from "@/lib/people/copy"

import { PeopleShell } from "./PeopleShell"
import "./styles/table.css"

const SKELETON_ROWS = 8

// The page's frame while the people load, laid out as the loaded page is so nothing moves
// when it arrives: the title, the search line, and shimmering rows.
export function PeopleLoading() {
  return (
    <PeopleShell
      main={
        <>
          <PageHeader title="People" data-head aria-busy="true" />
          <section className="bar" data-bar>
            <SearchField
              className="bar-search"
              data-search
              placeholder={SEARCH_PLACEHOLDER}
              aria-label={SEARCH_LABEL}
              disabled
            />
            <Select
              aria-label="Sharing"
              disabled
              value="all"
              onChange={() => {}}
              options={[{ value: "all", label: "All" }]}
            />
            <Select
              aria-label="Relationship"
              disabled
              value=""
              onChange={() => {}}
              options={[{ value: "", label: "All relationships" }]}
            />
          </section>
          <section className="people-grid" data-grid>
            <div className="grid-head" role="row" data-grid-head />
            <div className="grid-loading" aria-busy="true">
              <span className="sr-only">Loading people…</span>
              {Array.from({ length: SKELETON_ROWS }, (_, index) => (
                <Skeleton key={index} />
              ))}
            </div>
          </section>
        </>
      }
    />
  )
}
