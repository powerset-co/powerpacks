import type { RefObject } from "react"

import type { useSelection } from "@/hooks/useSelection"
import type { WorthAction } from "@/lib/people/facets"
import { TABS, type Tab } from "@/lib/people/view"
import type { Person } from "@/types/people"

import type { PeopleTableHandle } from "../table/PeopleTable"
import type { useDrawer } from "./useDrawer"
import { useKeyboard } from "./useKeyboard"

interface ShortcutTargets {
  // Off while another view covers the page (the Logbook reader).
  active: boolean
  search: RefObject<HTMLInputElement>
  table: RefObject<PeopleTableHandle>
  matching: readonly Person[]
  focus: number
  setFocus: (index: number) => void
  setTab: (tab: Tab) => void
  selection: ReturnType<typeof useSelection>
  drawer: ReturnType<typeof useDrawer>
  label: (action: WorthAction, targets: readonly string[]) => Promise<boolean>
  // The delete question and its answer (useDecisions).
  askDelete: (targets: readonly string[]) => void
  confirmDelete: () => void
  armed: () => boolean
  cancelDelete: () => void
  // The selection, else the open person (PeopleWorkspace).
  targets: readonly string[]
}

// The People page's keys wired to its state.
export function usePeopleShortcuts({
  active,
  search,
  table,
  matching,
  focus,
  setFocus,
  setTab,
  selection,
  drawer,
  label,
  askDelete,
  confirmDelete,
  armed,
  cancelDelete,
  targets,
}: ShortcutTargets) {
  const focused = matching[focus]
  const act = (action: WorthAction) => {
    if (targets.length) void label(action, targets)
  }

  useKeyboard(active, {
    focusSearch: () => {
      search.current?.focus()
      search.current?.select()
    },
    switchTab: (position) => setTab(TABS[position] ?? "all"),
    move: (step) => {
      if (!matching.length) return
      const next = Math.min(matching.length - 1, Math.max(0, focus + step))
      setFocus(next)
      table.current?.scrollToIndex(next, { align: "auto" })
    },
    toggleFocused: () => {
      if (!focused) return false
      selection.toggle(focused.parent_id)
      return true
    },
    selectAll: selection.toggleAll,
    worthYes: () => act("yes"),
    worthNo: () => act("no"),
    askDelete: () => {
      if (targets.length) askDelete(targets)
    },
    confirmDelete: () => {
      if (!armed()) return false
      confirmDelete()
      return true
    },
    openFocused: () => {
      if (focused) drawer.toggle(focused.parent_id)
    },
    escape: () => {
      if (armed()) cancelDelete()
      else if (drawer.openId) drawer.close()
      else if (selection.selected.size) selection.clear()
    },
  })
}
