import { useMemo } from "react"
import { Link } from "react-router-dom"

import { Drawer } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useDrawerSwap } from "@/hooks/useDrawerSwap"
import type { Person } from "@/types/people"

import type { DetailState } from "../hooks/usePersonDetail"
import "../styles/drawer.css"
import { DetailSections } from "./DetailSections"
import { DetailFailed, DetailLoading } from "./DetailStatus"
import { RefreshSignals } from "../head/RefreshSignals"
import { LogbookButton } from "./DrawerActions"
import { DrawerHeader } from "./DrawerHeader"
import { PersonFeedback } from "./PersonFeedback"
import type { SectionState } from "./sections"

interface PersonDrawerProps {
  // The person shown; stays set while the drawer animates shut.
  row: Person | null
  open: boolean
  section: SectionState
  detail: DetailState
  saving: boolean
  building: boolean
  onLogbook: () => void
  onView: (slugs: string[]) => void
  onClose: () => void
  onRetry: () => void
}

// The person in the shared drawer. Which sections are open carries across people.
export function PersonDrawer(props: PersonDrawerProps) {
  const { open, section, saving, building, onLogbook, onView, onClose, onRetry } = props
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
          <DrawerHeader row={row} detail={ready} action={<PersonFeedback row={row} />} onClose={onClose} />
          {row.in_progress ? <p className="dim">{row.name} is being updated; finish the run first.</p> : null}
          <div className="drawer-actions">
            {ready?.profile_review_url ? (
              <Button asChild shape="pill" size="sm">
                <Link to={ready.profile_review_url}>Check profile</Link>
              </Button>
            ) : null}
            <RefreshSignals people={[row.parent_id]} icon onDone={onRetry} />
            <LogbookButton
              row={row}
              disabled={saving || swap.leaving || row.in_progress}
              building={building}
              onBuild={onLogbook}
              onView={() => {
                if (row.logbook) onView([row.logbook])
              }}
            />
          </div>
          {detail.status === "loading" ? <DetailLoading /> : null}
          {detail.status === "failed" ? <DetailFailed onRetry={onRetry} /> : null}
          {ready ? <DetailSections row={row} detail={ready} section={section} onRefresh={onRetry} /> : null}
        </>
      ) : null}
    </Drawer>
  )
}
