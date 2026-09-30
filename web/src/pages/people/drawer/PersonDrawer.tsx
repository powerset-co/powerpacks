import { useCallback, useMemo, useState } from "react"

import { Drawer } from "@/components/shared"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import type { TagAction } from "@/lib/people/facets"
import { toggled } from "@/lib/sets"
import type { Person } from "@/types/people"

import type { DetailState } from "../hooks/usePersonDetail"
import "../styles/drawer.css"
import { DecisionSection } from "./DecisionSection"
import { DetailSections } from "./DetailSections"
import { DetailFailed, DetailLoading } from "./DetailStatus"
import { DrawerActions } from "./DrawerActions"
import { DrawerHeader } from "./DrawerHeader"
import { OPEN_BY_DEFAULT, type SectionKey, type SectionState } from "./sections"

interface PersonDrawerProps {
  // The person shown; stays set while the drawer animates shut.
  row: Person | null
  open: boolean
  detail: DetailState
  saving: boolean
  building: boolean
  onAction: (action: TagAction) => void
  onLogbook: () => void
  onClose: () => void
  onRetry: () => void
}

// The person in the shared drawer. Which sections are open carries across people.
export function PersonDrawer(props: PersonDrawerProps) {
  const { open, saving, building, onAction, onLogbook, onClose, onRetry } = props
  const [sections, setSections] = useState<ReadonlySet<SectionKey>>(OPEN_BY_DEFAULT)
  const next = useMemo(() => ({ row: props.row, detail: props.detail }), [props.row, props.detail])
  const swap = useDrawerSwap(props.row?.parent_id ?? null, next, open)
  const { row, detail } = swap.content

  const section: SectionState = useCallback(
    (key) => ({
      open: sections.has(key),
      onToggle: (next) => setSections((current) => toggled(current, key, next)),
    }),
    [sections],
  )

  const ready = detail.status === "ready" ? detail.detail : null
  return (
    <Drawer
      open={open}
      label="Person"
      contentKey={swap.shownId}
      leaving={swap.leaving}
      onTransitionEnd={swap.onTransitionEnd}
    >
      {row ? (
        <>
          <DrawerHeader row={row} detail={ready} onClose={onClose} />
          <DrawerActions
            row={row}
            saving={saving}
            disabled={saving || swap.leaving}
            building={building}
            onAction={onAction}
            onLogbook={onLogbook}
          />
          <DecisionSection row={row} detail={ready} {...section("decision")} />
          {detail.status === "loading" ? <DetailLoading /> : null}
          {detail.status === "failed" ? <DetailFailed onRetry={onRetry} /> : null}
          {ready ? <DetailSections row={row} detail={ready} section={section} /> : null}
        </>
      ) : null}
    </Drawer>
  )
}
