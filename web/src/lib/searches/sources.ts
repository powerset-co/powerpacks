// A candidate's network sources as the row and its evidence show them (rendering.py
// _network_sources): one pill per family, busiest first, email and message counts on theirs,
// and who each source came through.

import { CHANNELS, toChannel, type Channel } from "@/lib/channels"
import type { NetworkOperator, PersonAttribution } from "@/types/searches"

// The families counted in emails or messages; the others are connections with no count.
const COUNTED: ReadonlySet<Channel> = new Set(["gmail", "imessage", "whatsapp"])
const MESSAGE_CHANNELS: ReadonlySet<Channel> = new Set(["imessage", "whatsapp"])
const THOUSAND = 1000

export interface SourceFamily {
  channel: Channel
  interactions: number
  // "42", "~2k", or "" for a family without a count.
  count: string
}

/** rendering.py: "~2k" from a thousand up, else the number; nothing for zero. */
function interactionText(interactions: number): string {
  if (interactions <= 0) return ""
  return interactions >= THOUSAND ? `~${Math.round(interactions / THOUSAND)}k` : String(interactions)
}

/** One entry per pill family, summed over its sources, busiest first (ties keep source order). */
export function sourceFamilies(attribution: PersonAttribution | null): SourceFamily[] {
  const totals = new Map<Channel, number>()
  for (const source of attribution?.sources ?? []) {
    const channel = toChannel(source.channel)
    if (channel !== null) totals.set(channel, (totals.get(channel) ?? 0) + source.total_interactions)
  }
  return [...totals]
    .sort((a, b) => b[1] - a[1])
    .map(([channel, interactions]) => ({
      channel,
      interactions,
      count: COUNTED.has(channel) ? interactionText(interactions) : "",
    }))
}

/** An operator's line: the families they bring, then their email and message counts. */
export function operatorDetail(operator: NetworkOperator): string {
  const channels = [...new Set(operator.channels.map(toChannel).filter((channel) => channel !== null))]
  const counts = [
    channels.includes("gmail") && operator.gmail_interactions !== null
      ? `${operator.gmail_interactions.toLocaleString("en-US")} emails`
      : "",
    channels.some((channel) => MESSAGE_CHANNELS.has(channel)) && operator.message_interactions !== null
      ? `${operator.message_interactions.toLocaleString("en-US")} messages`
      : "",
  ]
  return [...channels.map((channel) => CHANNELS[channel].title), ...counts].filter(Boolean).join(" · ")
}
