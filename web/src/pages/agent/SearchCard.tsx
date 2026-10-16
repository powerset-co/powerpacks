import { useState } from "react"

import { CLOSE_MARK } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { PreparedSearch, SearchFilter, SearchTrait } from "@/lib/agent/preparedSearch"
import { send } from "@/lib/agent/store"
import { cn } from "@/lib/utils"

interface SearchCardProps {
  search: PreparedSearch
  // Editing, Execute and Modify: the newest card's, while the agent waits for an answer.
  actions: boolean
}

const CHIP = "rounded-full border border-line px-2.5 py-1 text-[11.5px] font-semibold leading-snug"
// A chip being edited: its value is a text field, and the chip shows the field's focus.
const EDIT_CHIP = cn(CHIP, "inline-flex max-w-full items-center gap-1 pr-1.5 focus-within:border-primary")
const REMOVE =
  "inline-grid size-4 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"

/**
 * A chip's value as a text field as wide as its text: a hidden copy of the text sizes the cell the
 * field fills, so the chip reads as it does when it is not being edited.
 */
function ChipField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <span className="inline-grid min-w-0">
      <span aria-hidden="true" className="invisible col-start-1 row-start-1 overflow-hidden whitespace-pre">
        {value}
      </span>
      <input
        aria-label={label}
        value={value}
        size={1}
        onChange={(event) => onChange(event.target.value)}
        className="col-start-1 row-start-1 w-full min-w-[1ch] bg-transparent text-foreground outline-none focus-visible:outline-none"
      />
    </span>
  )
}

/** `name: a; b`, or `name:` with nothing in it. */
function messageLine(name: string, parts: string[]): string {
  return parts.length > 0 ? `${name}: ${parts.join("; ")}` : `${name}:`
}

/**
 * What Execute sends for edits: every filter the search came with, in its order (one removed or
 * emptied as `key =`), then every trait (one emptied is left out). The skill writes them into the
 * payload and runs it. A search that came with no traits and still has none sends no traits line,
 * since an empty one tells the skill to clear the payload's traits.
 */
function modifyMessage(search: PreparedSearch, filters: SearchFilter[], traits: SearchTrait[]): string {
  const filterParts: string[] = []
  for (const { key } of search.filters) {
    const value = filters.find((filter) => filter.key === key)?.value.trim() ?? ""
    filterParts.push(value ? `${key} = ${value}` : `${key} =`)
  }
  const traitParts: string[] = []
  for (const trait of traits) {
    const value = trait.value.trim()
    if (value) traitParts.push(`${value} (${trait.temporal})`)
  }
  const message = ["Modify, then execute:", messageLine("filters", filterParts)]
  if (search.traits.length > 0 || traitParts.length > 0) message.push(messageLine("traits", traitParts))
  return message.join("\n")
}

/**
 * A prepared search in the chat: its query, traits, filters, pool and notes, and the run-or-change
 * choice. While it asks, the traits and the filters are edited in place and Execute sends the
 * edits; a blocked one shows what blocks it and can only be modified.
 */
export function SearchCard({ search, actions }: SearchCardProps) {
  const [filters, setFilters] = useState(search.filters)
  const [traits, setTraits] = useState(search.traits)
  const [newTrait, setNewTrait] = useState("")
  // A card that no longer asks shows the search as it was prepared.
  const shownFilters = actions ? filters : search.filters
  const shownTraits = actions ? traits : search.traits

  // The traits and the one typed into Add trait, so Execute keeps it without an Enter.
  function traitsWithTyped(): SearchTrait[] {
    const value = newTrait.trim()
    return value ? [...traits, { value, temporal: "current" }] : traits
  }

  function execute() {
    const edited = modifyMessage(search, filters, traitsWithTyped())
    void send(edited === modifyMessage(search, search.filters, search.traits) ? "execute" : edited)
  }

  function addTrait() {
    setTraits(traitsWithTyped())
    setNewTrait("")
  }

  return (
    <section
      aria-label="Search"
      className="rise-in flex flex-col gap-3 rounded-[var(--radius-m)] border border-line bg-card p-4 shadow-[var(--shadow-1)]"
    >
      <h2 className="m-0 text-[15px] font-semibold">{search.query}</h2>
      {(shownTraits.length > 0 || actions) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {shownTraits.length > 0 && (
            <ul aria-label="Traits" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
              {shownTraits.map((trait, index) =>
                actions ? (
                  // Index keys: a trait's value changes as it is typed.
                  <li key={index} className={EDIT_CHIP}>
                    <ChipField
                      label={`Trait (${trait.temporal})`}
                      value={trait.value}
                      onChange={(value) =>
                        setTraits(traits.map((each, at) => (at === index ? { ...each, value } : each)))
                      }
                    />
                    <span className="text-faint">({trait.temporal})</span>
                    <button
                      type="button"
                      aria-label={`Remove ${trait.value}`}
                      onClick={() => setTraits(traits.filter((_, at) => at !== index))}
                      className={REMOVE}
                    >
                      {CLOSE_MARK}
                    </button>
                  </li>
                ) : (
                  <li key={index} className={CHIP}>
                    {trait.value} <span className="text-faint">({trait.temporal})</span>
                  </li>
                ),
              )}
            </ul>
          )}
          {actions && (
            <input
              aria-label="Add trait"
              placeholder="Add trait"
              value={newTrait}
              onChange={(event) => setNewTrait(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") addTrait()
              }}
              className={cn(
                CHIP,
                "w-32 border-dashed bg-transparent text-foreground outline-none placeholder:text-faint focus:border-primary focus-visible:outline-none",
              )}
            />
          )}
        </div>
      )}
      {shownFilters.length > 0 && (
        <ul aria-label="Filters" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
          {shownFilters.map((filter) =>
            actions ? (
              <li key={filter.key} className={EDIT_CHIP}>
                <span className="shrink-0 text-faint">{filter.label}:</span>
                <ChipField
                  label={filter.label}
                  value={filter.value}
                  onChange={(value) =>
                    setFilters(filters.map((each) => (each.key === filter.key ? { ...each, value } : each)))
                  }
                />
                <button
                  type="button"
                  aria-label={`Remove ${filter.label}`}
                  onClick={() => setFilters(filters.filter((each) => each.key !== filter.key))}
                  className={REMOVE}
                >
                  {CLOSE_MARK}
                </button>
              </li>
            ) : (
              <li key={filter.key} className={CHIP}>
                <span className="text-faint">{filter.label}:</span> {filter.value}
              </li>
            ),
          )}
        </ul>
      )}
      {search.pool && (
        <p className="m-0 text-xs text-muted-foreground">
          Pool: {search.pool.matched.toLocaleString()} of {search.pool.total.toLocaleString()} people
        </p>
      )}
      {search.notes.length > 0 && (
        <ul
          aria-label="Notes"
          className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-muted-foreground"
        >
          {search.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {search.blockers.length > 0 && (
        <ul aria-label="Blockers" className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-warn">
          {search.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      )}
      {actions && (
        <div className="flex flex-wrap gap-2">
          {search.blockers.length === 0 && (
            <Button variant="primary" onClick={execute}>
              Execute
            </Button>
          )}
          <Button onClick={() => document.getElementById("agent-message")?.focus()}>Modify</Button>
        </div>
      )}
    </section>
  )
}
