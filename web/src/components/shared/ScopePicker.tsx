import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select"
import { offeredSets, useSearchScope } from "@/lib/agent/useSearchScope"
import type { ChatScope, SearchSet } from "@/types/searches"

const LOCAL = "local"

function name(set: SearchSet): string {
  return set.is_personal && set.owned ? "Personal network" : set.name
}

function people(set: SearchSet): string {
  return ` · ${set.person_count.toLocaleString()}`
}

/** The picker's value for a scope: the set id, or "local". */
function valueOf(scope: ChatScope | null): string {
  if (scope === null) return ""
  return "set_id" in scope ? scope.set_id : LOCAL
}

interface ScopePickerProps {
  value: ChatScope | null
  onChange: (scope: ChatScope) => void
  /** Shown when the value is no set on offer. */
  placeholder?: string
  disabled?: boolean
  title?: string
  /** The trigger's face: the chat's pill, a page's field. */
  className?: string
}

/** "Search in": one of your Powerset sets, or this Mac's local index when it has one (which then
 *  stands in for your cloud personal network). Nothing on offer, it is not drawn. */
export function ScopePicker({
  value,
  onChange,
  placeholder = "Pick a set",
  disabled,
  title,
  className,
}: ScopePickerProps) {
  const scope = useSearchScope()
  if (!scope.data) return null
  const { sets: all, local_db } = scope.data
  const sets = offeredSets(scope.data)
  if (sets.length === 0 && !local_db) return null
  const selected = valueOf(value)
  // Looked up among all sets, so a chat pinned to one no longer offered still shows its name.
  const picked = all.find(({ set_id }) => set_id === selected)
  const shown = selected === LOCAL ? "Local index" : picked ? name(picked) : placeholder
  const choose = (next: string) => {
    if (next === LOCAL) {
      if (local_db) onChange({ local_db })
    } else onChange({ set_id: next })
  }
  return (
    <Select value={selected} onValueChange={choose} disabled={disabled}>
      <SelectTrigger aria-label="Search in" title={title} className={className}>
        {/* A long name gives way before its count does. */}
        <span className="flex min-w-0">
          <span className="truncate">{shown}</span>
          {picked && <span className="shrink-0 whitespace-pre">{people(picked)}</span>}
        </span>
      </SelectTrigger>
      <SelectContent align="start">
        {sets.length > 0 && (
          <SelectGroup>
            <SelectLabel>Powerset</SelectLabel>
            {sets.map((set) => (
              <SelectItem key={set.set_id} value={set.set_id}>
                {name(set) + people(set)}
              </SelectItem>
            ))}
          </SelectGroup>
        )}
        {local_db && (
          <SelectGroup>
            <SelectLabel>This Mac</SelectLabel>
            <SelectItem value={LOCAL}>Local index</SelectItem>
          </SelectGroup>
        )}
      </SelectContent>
    </Select>
  )
}
