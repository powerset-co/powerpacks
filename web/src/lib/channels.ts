// The source families that have a pill: their names and pill colours, one home for every page.

export type Channel = "gmail" | "imessage" | "whatsapp" | "linkedin" | "x" | "csv_import"

export interface ChannelMeta {
  title: string
  // people.css .source[data-c=…] and results.css .network-source[data-channel=…]: tinted
  // fill, bright glyph, half-strength border.
  colors: string
}

export const CHANNELS: Readonly<Record<Channel, ChannelMeta>> = {
  gmail: { title: "Gmail", colors: "bg-[#22c55e33] text-[#4ade80] border-[#22c55e4d]" },
  imessage: { title: "iMessage", colors: "bg-[#10b98133] text-[#34d399] border-[#10b9814d]" },
  whatsapp: { title: "WhatsApp", colors: "bg-[#25d36633] text-[#25d366] border-[#25d3664d]" },
  linkedin: { title: "LinkedIn", colors: "bg-[#3b82f633] text-[#60a5fa] border-[#3b82f64d]" },
  x: { title: "X", colors: "bg-[#262626] text-[#f5f5f5] border-[#525252]" },
  csv_import: { title: "Contacts export", colors: "bg-[#f9731633] text-[#fb923c] border-[#f973164d]" },
}

// Source names the search server sends that draw another family's pill: rendering.py shows
// twitter as X and phone under Messages, whose glyph is iMessage's.
const ALIASES: Readonly<Record<string, Channel>> = { twitter: "x", phone: "imessage" }

function isChannel(value: string): value is Channel {
  return Object.hasOwn(CHANNELS, value)
}

/** The family whose pill a source name draws, or null for a source with no pill. */
export function toChannel(value: string): Channel | null {
  return isChannel(value) ? value : (ALIASES[value] ?? null)
}

/** A row's source names as pill families, once each in first-seen order. The servers pass an
 *  unmapped channel name through unchanged (share model.py `_families`), so rows stay `string[]`. */
export function toChannels(values: readonly string[]): Channel[] {
  return [...new Set(values.map(toChannel).filter((channel) => channel !== null))]
}
