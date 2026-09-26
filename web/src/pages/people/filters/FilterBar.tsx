import { forwardRef } from "react";

import { SearchField } from "@/components/shared";
import { plural } from "@/lib/people/copy";

import { ActiveChips } from "./ActiveChips";

export const SEARCH_PLACEHOLDER = "Search name, title, company, location";
export const SEARCH_LABEL = "Search people";

interface FilterBarProps {
  text: string;
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  shown: number;
  inTab: number;
  onText: (text: string) => void;
  onRemove: (key: string, value: string) => void;
  onClear: () => void;
}

// Search box, the held facet chips, and how many people are showing.
export const FilterBar = forwardRef<HTMLInputElement, FilterBarProps>(
  ({ text, filters, shown, inTab, onText, onRemove, onClear }, searchRef) => (
    <section className="bar" data-bar>
      <SearchField
        ref={searchRef}
        className="bar-search"
        data-search
        value={text}
        placeholder={SEARCH_PLACEHOLDER}
        aria-label={SEARCH_LABEL}
        onChange={(event) => onText(event.target.value)}
      />
      <ActiveChips filters={filters} onRemove={onRemove} onClear={onClear} />
      <span className="bar-count num" data-count aria-live="polite">
        {shown === inTab ? plural(inTab, "person") : `${shown.toLocaleString()} of ${inTab.toLocaleString()} people`}
      </span>
    </section>
  ),
);
FilterBar.displayName = "FilterBar";
