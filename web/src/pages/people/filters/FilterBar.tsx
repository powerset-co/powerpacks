import { ChevronDown } from "lucide-react"
import { forwardRef, memo, type ReactNode } from "react"

import { SearchField, Select } from "@/components/shared"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { countOf } from "@/lib/copy"
import { must } from "@/lib/must"
import { label, SEARCH_LABEL, SEARCH_PLACEHOLDER } from "@/lib/people/copy"
import { facetOf, facetText, HIDES, type HideKey } from "@/lib/people/facets"
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
  // The Filters menu: which kinds of people are hidden, and the switch for one.
  hide: ReadonlySet<HideKey>
  onHide: (key: HideKey) => void
  // A line before the count while something the filters read is unavailable.
  notice: ReactNode
  // Refresh signals and Share network, at the line's right end.
  actions: ReactNode
}

const KIND = facetOf("relationship_kind")

// The Filters menu: one switch per HIDES entry; the button counts the ones on.
function FiltersMenu({ hide, onHide }: { hide: ReadonlySet<HideKey>; onHide: (key: HideKey) => void }) {
  return (
    <Popover>
      <PopoverTrigger
        aria-label="Filters"
        data-filters
        className="flex h-8 items-center gap-2 rounded-[var(--radius-s)] border border-line-strong bg-background pl-2.5 pr-2 text-[13px] text-foreground"
      >
        {hide.size ? `Filters · ${hide.size}` : "Filters"}
        <ChevronDown className="h-4 w-4 opacity-50" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto min-w-56 p-2">
        {HIDES.map(({ key, label: text }) => (
          <Label
            key={key}
            className="flex cursor-pointer items-center gap-2.5 rounded-[var(--radius-s)] px-2 py-2 text-[13px] font-normal hover:bg-muted"
          >
            <Checkbox checked={hide.has(key)} onCheckedChange={() => onHide(key)} />
            {text}
          </Label>
        ))}
      </PopoverContent>
    </Popover>
  )
}

// One line: the search box, the worth call (or all), the relationship kind, how many show
// once the search or the kind narrows the list, then the page's actions on the right.
export const FilterBar = memo(
  forwardRef<HTMLInputElement, FilterBarProps>(
    (
      {
        text,
        tab,
        totals,
        relationship,
        kinds,
        shown,
        onText,
        onTab,
        onRelationship,
        hide,
        onHide,
        notice,
        actions,
      },
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
            aria-label="Decision"
            value={tab}
            onChange={(value) => onTab(must(TABS.find((option) => option === value)))}
            options={TABS.filter((option) => option === "all" || option === tab || totals[option] > 0).map(
              (option) => ({
                value: option,
                label: `${option === "all" ? "All" : label("worth", option)} · ${totals[option].toLocaleString()}`,
              }),
            )}
          />
          <Select
            aria-label="Relationship"
            value={relationship}
            onChange={onRelationship}
            options={[
              { value: "", label: "All relationships" },
              ...options.map(({ kind, text: name, count }) => ({
                value: kind,
                label: `${name} · ${count.toLocaleString()}`,
              })),
            ]}
          />
          <FiltersMenu hide={hide} onHide={onHide} />
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
