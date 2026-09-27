import { useState } from "react"

import { Appear, Chip } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { usePresenceList } from "@/hooks/usePresenceList"
import { toggled } from "@/lib/sets"
import { cn } from "@/lib/utils"

interface TagFiltersProps {
  tags: readonly string[]
  selected: ReadonlySet<string>
  onChange: (selected: ReadonlySet<string>) => void
}

const sameTag = (tag: string) => tag

// Within "Tagged": a chip per tag in the search (any selected tag matches), "Clear filter".
// Chips present when the group appears arrive with it; a tag made later rises in alone, and a
// tag deleted from the search leaves with the .rise exit.
export function TagFilters({ tags, selected, onChange }: TagFiltersProps) {
  const [first] = useState(() => new Set(tags))
  const chips = usePresenceList(tags, sameTag)
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" role="group" aria-label="Tag filter">
      <span className="toolbar-label">Filter:</span>
      {chips.map((chip) => (
        <Chip
          key={chip.key}
          pressed={selected.has(chip.item)}
          data-open={chip.open}
          onTransitionEnd={chip.onTransitionEnd}
          className={cn("rise", first.has(chip.key) && "rise-settled")}
          onClick={() => onChange(toggled(selected, chip.item))}
        >
          {chip.item}
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
