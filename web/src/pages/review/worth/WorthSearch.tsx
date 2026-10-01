import { useState, type KeyboardEvent } from "react"

import type { WorthPendingEntry } from "@/types/review"

import { SEARCH } from "./copy"

/** The list never grows past this many names. */
const LISTED = 8

/** What an arrow key adds to the highlighted position. */
const ARROW_STEP: ReadonlyMap<string, number> = new Map([
  ["ArrowDown", 1],
  ["ArrowUp", -1],
])

interface WorthSearchProps {
  /** The people still pending, in queue order. */
  names: readonly WorthPendingEntry[]
  /** A person was chosen: the queue shows their card. */
  onPick: (key: string) => void
}

function matching(names: readonly WorthPendingEntry[], needle: string): WorthPendingEntry[] {
  return names.filter((entry) => entry.name.toLowerCase().includes(needle)).slice(0, LISTED)
}

// The typeahead over the pending people: type part of a name, pick the person from the list.
// The arrows move the highlight, Enter picks it, Escape clears the box, and leaving the box
// closes the list.
export function WorthSearch({ names, onPick }: WorthSearchProps) {
  const [text, setText] = useState("")
  /** The box is in use; the list shows once it holds something to look for. */
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)

  const needle = text.trim().toLowerCase()
  const listed = open && needle !== ""
  const matches = listed ? matching(names, needle) : []

  const look = (value: string) => {
    setText(value)
    setOpen(true)
    setActive(0)
  }
  const clear = () => {
    setOpen(false)
    setText("")
  }
  const pick = (entry: WorthPendingEntry) => {
    clear()
    onPick(entry.key)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const step = ARROW_STEP.get(event.key)
    if (step !== undefined) {
      event.preventDefault()
      if (matches.length) setActive((active + step + matches.length) % matches.length)
      return
    }
    if (event.key === "Enter") {
      event.preventDefault()
      const entry = matches[active]
      if (entry) pick(entry)
      return
    }
    if (event.key === "Escape") {
      event.preventDefault()
      clear()
    }
  }

  return (
    <div className="worth-search">
      <input
        className="worth-search-input"
        type="search"
        placeholder={SEARCH.placeholder}
        aria-label={SEARCH.label}
        autoComplete="off"
        spellCheck={false}
        value={text}
        onChange={(event) => look(event.target.value)}
        onFocus={() => look(text)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
      />
      <ul className="worth-search-list" role="listbox" hidden={!listed}>
        {matches.map((entry, position) => (
          <li
            key={entry.key}
            role="option"
            aria-selected={position === active}
            tabIndex={-1}
            className={position === active ? "active" : undefined}
            // mousedown beats the input's blur, so a click still picks.
            onMouseDown={(event) => {
              event.preventDefault()
              pick(entry)
            }}
          >
            {entry.name}
          </li>
        ))}
        {listed && !matches.length ? <li className="worth-search-empty">{SEARCH.empty}</li> : null}
      </ul>
    </div>
  )
}
