import { forwardRef, memo, type ReactNode } from "react"

import { SearchField, Select } from "@/components/shared"
import { countOf } from "@/lib/copy"
import { must } from "@/lib/must"
import { label, SEARCH_LABEL, SEARCH_PLACEHOLDER } from "@/lib/people/copy"
import { facetOf, facetText } from "@/lib/people/facets"
import { TABS, type Tab } from "@/lib/people/view"

interface FilterBarProps {
  text: string
  tab: Tab
  totals: Readonly<Record<Tab, number>>
  // The relationship kind held, "" for all; and how many in the tab hold each kind.
  relationship: string
  kinds: ReadonlyMap<string, number>
  shown: number
  onText: (text: string) => void
  onTab: (tab: Tab) => void
  onRelationship: (kind: string) => void
  // A line before the count while something the filters read is unavailable.
  notice: ReactNode
  // Refresh signals and Share network, at the line's right end.
  actions: ReactNode
}

const KIND = facetOf("relationship_kind")

// One line: the search box, the sharing decision (or all), the relationship kind, how many show
// once the search or the kind narrows the list, then the page's actions on the right.
export const FilterBar = memo(
  forwardRef<HTMLInputElement, FilterBarProps>(
    (
      { text, tab, totals, relationship, kinds, shown, onText, onTab, onRelationship, notice, actions },
      searchRef,
    ) => {
      // By name, N/A last; the held kind stays listed when this tab has none of it.
      const options = [...new Set([...kinds.keys(), ...(relationship ? [relationship] : [])])]
        .map((kind) => ({ kind, text: facetText(KIND, kind), count: kinds.get(kind) ?? 0 }))
        .sort(
          (a, b) =>
            Number(a.kind === "unknown") - Number(b.kind === "unknown") || a.text.localeCompare(b.text),
        )
      return (
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
          <Select
            aria-label="Sharing"
            value={tab}
            onChange={(event) => onTab(must(TABS.find((option) => option === event.target.value)))}
          >
            {TABS.map((option) => (
              <option key={option} value={option}>
                {`${option === "all" ? "All" : label("share", option)} · ${totals[option].toLocaleString()}`}
              </option>
            ))}
          </Select>
          <Select
            aria-label="Relationship"
            value={relationship}
            onChange={(event) => onRelationship(event.target.value)}
          >
            <option value="">All relationships</option>
            {options.map(({ kind, text: name, count }) => (
              <option key={kind} value={kind}>
                {`${name} · ${count.toLocaleString()}`}
              </option>
            ))}
          </Select>
          {notice}
          <span className="bar-count num" data-count aria-live="polite">
            {shown === totals[tab] ? "" : countOf(shown, totals[tab], "person")}
          </span>
          <div className="bar-actions">{actions}</div>
        </section>
      )
    },
  ),
)
FilterBar.displayName = "FilterBar"
