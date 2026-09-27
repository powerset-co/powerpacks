import { useCallback, useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react"
import { createPortal } from "react-dom"

import { SourcePill } from "@/components/shared"
import { usePresence } from "@/hooks/usePresence"
import { CHANNELS, toChannel, type Channel } from "@/lib/channels"
import { sourceFamilies, type SourceFamily } from "@/lib/searches/sources"
import type { NetworkOperator, PersonAttribution } from "@/types/searches"

import { useFloatPanel } from "../hooks/useFloatPanel"
import { ConnectedVia, Operators } from "./Operators"

const POPOVER_WIDTH = 288
// results.js: a popover the pointer opened closes this long after the pointer has left it.
const HOVER_CLOSE_MS = 150
// The families counted in emails or messages (lib/searches/sources.ts COUNTED).
const UNIT: Partial<Record<Channel, string>> = { gmail: "emails", imessage: "messages", whatsapp: "messages" }

interface NetworkSourcesProps {
  attribution: PersonAttribution | null
  name: string
}

/**
 * rendering.py _network_sources: one pill per source family, busiest first, then the stack of
 * people the person came through. Each opens who brought that source, with their counts,
 * on hover or click.
 */
export function NetworkSources({ attribution, name }: NetworkSourcesProps) {
  const families = sourceFamilies(attribution)
  const operators = attribution?.operators ?? []
  if (!families.length && !operators.length) return null
  return (
    <span className="result-network">
      <span className="result-sources">
        {families.map((family) => (
          <NetworkTrigger
            key={family.channel}
            label={`${CHANNELS[family.channel].title} sources for ${name}`}
            source={family.channel}
            panel={<SourceDetail family={family} operators={operators} />}
          >
            <SourcePill channel={family.channel} size="sm" count={family.count} />
          </NetworkTrigger>
        ))}
      </span>
      {operators.length ? (
        <NetworkTrigger label={`Source operators for ${name}`} panel={<ConnectedVia operators={operators} />}>
          <Operators operators={operators} />
        </NetworkTrigger>
      ) : null}
    </span>
  )
}

interface SourceDetailProps {
  family: SourceFamily
  operators: readonly NetworkOperator[]
}

// One family: its total, then who brought it with their share; Gmail lists the accounts.
function SourceDetail({ family, operators }: SourceDetailProps) {
  const { channel } = family
  const unit = UNIT[channel]
  const through = operators.filter((operator) =>
    operator.channels.some((source) => toChannel(source) === channel),
  )
  return (
    <>
      <p className="network-title">
        <strong>{CHANNELS[channel].title}</strong>
        {unit ? (
          <small>
            {family.interactions.toLocaleString("en-US")} {unit}
          </small>
        ) : null}
      </p>
      <ConnectedVia
        operators={through}
        detail={(operator) => share(operator, channel)}
        accounts={channel === "gmail"}
      />
    </>
  )
}

/** An operator's count on one family: "42 emails", "7 messages", or nothing for a connection. */
function share(operator: NetworkOperator, channel: Channel): string {
  const unit = UNIT[channel]
  if (!unit) return ""
  const count = channel === "gmail" ? operator.gmail_interactions : operator.message_interactions
  return count === null ? "" : `${count.toLocaleString("en-US")} ${unit}`
}

interface NetworkTriggerProps {
  label: string
  // The family the trigger stands for; none marks the operator stack.
  source?: Channel
  panel: ReactNode
  children: ReactNode
}

// The button around a pill or the stack, and its popover: the pointer opens it and it leaves
// with the pointer; a click (or a key) keeps it until Escape, a press outside or a scroll.
function NetworkTrigger({ label, source, panel, children }: NetworkTriggerProps) {
  const [pinned, setPinned] = useState(false)
  const unpin = useCallback(() => setPinned(false), [])
  const { anchor, panel: box, place, open, show, hide } = useFloatPanel(POPOVER_WIDTH, unpin)
  const presence = usePresence(place)
  const timer = useRef<number | undefined>(undefined)
  const stay = useCallback(() => window.clearTimeout(timer.current), [])
  useEffect(() => stay, [stay])

  const leave = () => {
    stay()
    if (!pinned) timer.current = window.setTimeout(hide, HOVER_CLOSE_MS)
  }
  const enter = (event: PointerEvent<HTMLElement>) => {
    stay()
    if (event.pointerType === "mouse") show()
  }
  const click = () => {
    setPinned(!pinned)
    if (pinned) hide()
    else show()
  }

  return (
    <span className="network-attribution" onPointerEnter={enter} onPointerLeave={leave}>
      <button
        ref={anchor}
        type="button"
        className="network-trigger"
        aria-expanded={open}
        aria-label={label}
        data-source={source}
        data-network-operators={source === undefined ? "" : undefined}
        onClick={click}
      >
        {children}
      </button>
      {presence.mounted && presence.shown
        ? createPortal(
            <div
              ref={box}
              role="region"
              aria-label={label}
              className="float-panel rise network-popover fixed"
              data-open={presence.open}
              style={presence.shown}
              onTransitionEnd={presence.onTransitionEnd}
              onPointerEnter={stay}
              onPointerLeave={leave}
            >
              {panel}
            </div>,
            document.body,
          )
        : null}
    </span>
  )
}
