import { useState, type TransitionEvent } from "react"

import { useReducedMotion } from "./useReducedMotion"

export interface Presence<T> {
  key: string
  item: T
  // False while the item leaves: render it with data-open={open} and pass onTransitionEnd.
  open: boolean
  onTransitionEnd: (event: TransitionEvent<HTMLElement>) => void
}

interface Entry<T> {
  key: string
  item: T
  open: boolean
}

// Entries are the same list when their keys and open flags line up; items are read fresh by key.
function same<T>(a: readonly Entry<T>[], b: readonly Entry<T>[]): boolean {
  return (
    a.length === b.length &&
    a.every((entry, i) => {
      const other = b[i]
      return other?.key === entry.key && other.open === entry.open
    })
  )
}

/**
 * Keeps removed items mounted through their exit transition, where they were. Render each
 * entry with `data-open={open}` and its `onTransitionEnd`; the `.rise` rule in index.css
 * animates both ways. Under reduced motion no transition runs, so removed items drop at once.
 */
export function usePresenceList<T>(items: readonly T[], keyOf: (item: T) => string): Presence<T>[] {
  const reduced = useReducedMotion()
  const [shown, setShown] = useState<readonly Entry<T>[]>(() =>
    items.map((item) => ({ key: keyOf(item), item, open: true })),
  )

  const current = new Map(items.map((item) => [keyOf(item), item]))
  const next: Entry<T>[] = []
  for (const entry of shown) {
    const item = current.get(entry.key)
    if (item !== undefined) next.push({ key: entry.key, item, open: true })
    else if (!reduced) next.push({ ...entry, open: false })
  }
  const kept = new Set(next.map((entry) => entry.key))
  for (const [key, item] of current) {
    if (!kept.has(key)) next.push({ key, item, open: true })
  }
  if (!same(shown, next)) setShown(next)

  const drop = (key: string) => {
    setShown((entries) => entries.filter((entry) => entry.key !== key || entry.open))
  }
  return next.map((entry) => ({
    ...entry,
    onTransitionEnd: (event) => {
      if (!entry.open && event.target === event.currentTarget) drop(entry.key)
    },
  }))
}
