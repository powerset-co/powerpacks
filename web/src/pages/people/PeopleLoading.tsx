import { SearchField } from "@/components/shared";
import { Skeleton } from "@/components/ui/skeleton";

import { PeopleShell } from "./PeopleShell";
import { SEARCH_LABEL, SEARCH_PLACEHOLDER } from "./filters/FilterBar";
import "./styles/table.css";

const SKELETON_ROWS = 8;

// The page's frame while the people load: the search box and shimmering rows.
export function PeopleLoading() {
  return (
    <PeopleShell
      drawerOpen={false}
      rail={null}
      main={
        <>
          <section className="people-head" data-head />
          <section className="quick" data-quick aria-label="Quick filters" />
          <section className="bar" data-bar>
            <SearchField className="bar-search" data-search placeholder={SEARCH_PLACEHOLDER} aria-label={SEARCH_LABEL} disabled />
          </section>
          <section className="people-grid" data-grid>
            <div className="grid-head" role="row" data-grid-head />
            <div className="grid-loading" aria-busy="true">
              <span className="sr-only">Loading people…</span>
              {Array.from({ length: SKELETON_ROWS }, (_, index) => <Skeleton key={index} />)}
            </div>
          </section>
        </>
      }
    />
  );
}
