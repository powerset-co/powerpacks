import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useId, useRef, useState, type MouseEvent, type PointerEvent } from "react"
import { createPortal } from "react-dom"

import { SourcePills } from "@/components/shared"
import type { Channel } from "@/lib/channels"
import { useFloatPanel } from "@/hooks/useFloatPanel"
import { usePresence } from "@/hooks/usePresence"
import { fetchPersonDetail } from "@/lib/api/people"
import { toChannels } from "@/lib/channels"
import type { Person, PersonDetail } from "@/types/people"

const PANEL_WIDTH = 260
// A panel the pointer opened closes this long after the pointer leaves it (NetworkSources' delay).
const HOVER_CLOSE_MS = 150

// The pills an address or number sits beside (drawer/DrawerHeader): emails by Gmail's, numbers by
// iMessage's and WhatsApp's.
const EMAIL_PILLS: readonly Channel[] = ["gmail"]
const PHONE_PILLS: readonly Channel[] = ["imessage", "whatsapp"]

// One kind of contact: its name, the row's pills of that kind with their counts, then the values.
function Contact({
  title,
  pills,
  values,
  detail,
}: {
  title: string
  pills: readonly Channel[]
  values: readonly string[]
  detail: PersonDetail
}) {
  if (!pills.length && !values.length) return null
  const counts = Object.fromEntries(
    pills.map((channel) => [channel, (detail.interactions[channel] ?? 0).toLocaleString("en-US")]),
  )
  return (
    <div className="contact-kind">
      <p className="contact-title">
        <strong>{title}</strong>
        <SourcePills channels={pills} counts={counts} />
      </p>
      {values.length ? (
        values.map((value) => <span key={value}>{value}</span>)
      ) : (
        <span className="dim">None saved.</span>
      )}
    </div>
  )
}

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
  // The panel was placed while it said Loading: place it again for its real height.
  const { data } = detail
  useEffect(() => {
    if (data && open) show()
  }, [data, open, show])
  const panelId = useId()

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
  const channels = toChannels(row.channels)
  const kinds = detail.data
    ? [
        {
          title: "Email",
          pills: EMAIL_PILLS.filter((pill) => channels.includes(pill)),
          values: detail.data.emails,
        },
        {
          title: "Phone",
          pills: PHONE_PILLS.filter((pill) => channels.includes(pill)),
          values: detail.data.phones,
        },
      ].filter((kind) => kind.pills.length || kind.values.length)
    : []
  let body
  if (detail.isError) body = <span className="dim">Couldn't read their email or phone.</span>
  else if (!detail.data) body = <span className="dim">Loading…</span>
  else if (!kinds.length) body = <span className="dim">No email or phone saved.</span>
  else body = kinds.map((kind) => <Contact key={kind.title} {...kind} detail={detail.data} />)

  return (
    <span role="cell" className="sources c-sources" onPointerEnter={enter} onPointerLeave={leave}>
      <button
        ref={anchor}
        type="button"
        className="contact-trigger"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-describedby={open ? panelId : undefined}
        aria-label={label}
        onClick={click}
      >
        <SourcePills channels={channels} />
      </button>
      {presence.mounted && presence.shown
        ? createPortal(
            // A portal still bubbles to the row's click: selecting an address must not open the drawer.
            // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- the region only stops propagation
            <div
              ref={panel}
              id={panelId}
              role="region"
              aria-label={label}
              onClick={(event) => event.stopPropagation()}
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
