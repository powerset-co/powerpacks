import { useMemo } from "react"

import { Drawer } from "@/components/shared"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import type { TagAction } from "@/lib/people/facets"
import type { Person } from "@/types/people"

import type { DetailState } from "../hooks/usePersonDetail"
import "../styles/drawer.css"
import { DecisionSection } from "./DecisionSection"
import { DetailSections } from "./DetailSections"
import { DetailFailed, DetailLoading } from "./DetailStatus"
import { DrawerActions } from "./DrawerActions"
import { DrawerHeader } from "./DrawerHeader"
import type { SectionState } from "./sections"

interface PersonDrawerProps {
  // The person shown; stays set while the drawer animates shut.
  row: Person | null
  open: boolean
  section: SectionState
  detail: DetailState
  saving: boolean
  building: boolean
  onAction: (action: TagAction) => void
  onLogbook: () => void
  onView: (slugs: string[]) => void
  onClose: () => void
  onRetry: () => void
}

// The person in the shared drawer. Which sections are open carries across people.
export function PersonDrawer(props: PersonDrawerProps) {
  const { open, section, saving, building, onAction, onLogbook, onView, onClose, onRetry } = props
  const next = useMemo(() => ({ row: props.row, detail: props.detail }), [props.row, props.detail])
  const swap = useDrawerSwap(props.row?.parent_id ?? null, next, open)
  const { row, detail } = swap.content

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
          {row.in_progress ? <p className="dim">{row.name} is being updated; finish the run first.</p> : null}
          <DrawerActions
            row={row}
            saving={saving}
            disabled={saving || swap.leaving || row.in_progress}
            building={building}
            onLogbook={onLogbook}
            onView={() => {
              if (row.logbook) onView([row.logbook])
            }}
            onAction={onAction}
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
