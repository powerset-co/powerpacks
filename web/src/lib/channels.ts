// The source families that have a pill: their names and pill colours, one home for every page.

export type Channel = "gmail" | "imessage" | "whatsapp" | "linkedin"

export interface ChannelMeta {
  title: string
  // people.css .source[data-c=…]: tinted fill, bright glyph, half-strength border.
  colors: string
}

export const CHANNELS: Readonly<Record<Channel, ChannelMeta>> = {
  gmail: { title: "Gmail", colors: "bg-[#22c55e33] text-[#4ade80] border-[#22c55e4d]" },
  imessage: { title: "iMessage", colors: "bg-[#10b98133] text-[#34d399] border-[#10b9814d]" },
  whatsapp: { title: "WhatsApp", colors: "bg-[#10b98133] text-[#34d399] border-[#10b9814d]" },
  linkedin: { title: "LinkedIn", colors: "bg-[#3b82f633] text-[#60a5fa] border-[#3b82f64d]" },
}

/** A row's source names, keeping only the families that have a pill. The server passes an
 *  unmapped channel name through unchanged (model.py `_families`), so rows stay `string[]`. */
export function toChannels(values: readonly string[]): Channel[] {
  return values.filter((value): value is Channel => Object.hasOwn(CHANNELS, value))
}
