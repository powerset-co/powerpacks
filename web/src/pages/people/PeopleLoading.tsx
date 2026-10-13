import { Chip, SearchField } from "@/components/shared"
import { Skeleton } from "@/components/ui/skeleton"
import { label, SEARCH_LABEL, SEARCH_PLACEHOLDER } from "@/lib/people/copy"
import { QUICK } from "@/lib/people/facets"
import { ORDER } from "@/types/people"

import { PeopleShell } from "./PeopleShell"
import "./styles/table.css"

const SKELETON_ROWS = 8

// The page's frame while the people load, laid out as the loaded page is so nothing moves
// when it arrives: the decision tabs, quick filters, the search box, and shimmering rows.
export function PeopleLoading() {
  return (
    <PeopleShell
      main={
        <>
          <section className="people-head" data-head aria-busy="true">
            {ORDER.map((decision) => (
              <span key={decision} className="stat">
                <b>
                  <Skeleton className="h-[22px] w-12" />
                </b>
                <span>{label("share", decision)}</span>
              </span>
            ))}
          </section>
          <section className="quick" data-quick aria-label="Quick filters">
            {QUICK.map((quick) => (
              <Chip key={quick.name} className="chip" pressed={false} disabled>
                {quick.name}
              </Chip>
            ))}
          </section>
          <section className="bar" data-bar>
            <SearchField
              className="bar-search"
              data-search
              placeholder={SEARCH_PLACEHOLDER}
              aria-label={SEARCH_LABEL}
              disabled
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
