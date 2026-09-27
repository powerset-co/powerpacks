import { useState } from "react"

import { Chip } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { toggled } from "@/lib/sets"

import { Appear, RISE_IN } from "./Appear"
import { GROUP_LABEL } from "./styles"

interface TagFiltersProps {
  tags: readonly string[]
  selected: ReadonlySet<string>
  onChange: (selected: ReadonlySet<string>) => void
}

// Within "Tagged": a chip per tag in the search (any selected tag matches), "Clear filter".
export function TagFilters({ tags, selected, onChange }: TagFiltersProps) {
  // Chips present when the group appears arrive with it; a tag made later rises in alone.
  const [first] = useState(() => new Set(tags))
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" role="group" aria-label="Tag filter">
      <span className={GROUP_LABEL}>Filter:</span>
      {tags.map((tag) => (
        <Chip
          key={tag}
          pressed={selected.has(tag)}
          className={first.has(tag) ? undefined : RISE_IN}
          onClick={() => onChange(toggled(selected, tag))}
        >
          {tag}
        </Chip>
      ))}
      <Appear show={selected.size > 0}>
        <Button variant="ghost" size="sm" shape="pill" onClick={() => onChange(new Set())}>
          Clear filter
        </Button>
      </Appear>
    </span>
  )
}
