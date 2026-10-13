import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useRef, useState, type MouseEvent, type PointerEvent } from "react"
import { createPortal } from "react-dom"

import { SourcePills } from "@/components/shared"
import { useFloatPanel } from "@/hooks/useFloatPanel"
import { usePresence } from "@/hooks/usePresence"
import { fetchPersonDetail } from "@/lib/api/people"
import { toChannels } from "@/lib/channels"
import type { Person } from "@/types/people"

const PANEL_WIDTH = 260
// A panel the pointer opened closes this long after the pointer leaves it (NetworkSources' delay).
const HOVER_CLOSE_MS = 150

// The row's source pills. Hovering them, or a click that keeps it open, shows the person's
// emails and numbers, read from their detail the first time.
export function ContactSources({ row }: { row: Person }) {
  const [pinned, setPinned] = useState(false)
  const unpin = useCallback(() => setPinned(false), [])
  const { anchor, panel, place, open, show, hide } = useFloatPanel(PANEL_WIDTH, unpin)
  const presence = usePresence(place)
  const timer = useRef<number | undefined>(undefined)
  const stay = useCallback(() => window.clearTimeout(timer.current), [])
  useEffect(() => stay, [stay])
  const detail = useQuery({
    queryKey: ["person-contact", row.parent_id],
    queryFn: ({ signal }) => fetchPersonDetail(row.parent_id, signal),
    enabled: open,
    staleTime: Infinity,
  })

  const leave = () => {
    stay()
    if (!pinned) timer.current = window.setTimeout(hide, HOVER_CLOSE_MS)
  }
  const enter = (event: PointerEvent<HTMLElement>) => {
    stay()
    if (event.pointerType === "mouse") show()
  }
  // A click on the row opens the drawer; one on the pills only pins their panel.
  const click = (event: MouseEvent) => {
    event.stopPropagation()
    setPinned(!pinned)
    if (pinned) hide()
    else show()
  }

  const label = `Email and phone for ${row.name}`
  const values = detail.data ? [...detail.data.emails, ...detail.data.phones] : []
  let body
  if (detail.isError) body = <span className="dim">Couldn't read their email or phone.</span>
  else if (!detail.data) body = <span className="dim">Loading…</span>
  else if (!values.length) body = <span className="dim">No email or phone saved.</span>
  else body = values.map((value) => <span key={value}>{value}</span>)

  return (
    <span role="cell" className="sources c-sources" onPointerEnter={enter} onPointerLeave={leave}>
      <button
        ref={anchor}
        type="button"
        className="contact-trigger"
        aria-expanded={open}
        aria-label={label}
        onClick={click}
      >
        <SourcePills channels={toChannels(row.channels)} />
      </button>
      {presence.mounted && presence.shown
        ? createPortal(
            <div
              ref={panel}
              role="region"
              aria-label={label}
              className="float-panel rise contact-popover fixed"
              data-open={presence.open}
              style={presence.shown}
              onTransitionEnd={presence.onTransitionEnd}
              onPointerEnter={stay}
              onPointerLeave={leave}
            >
              {body}
            </div>,
            document.body,
          )
        : null}
    </span>
  )
}
