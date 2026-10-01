import { CHANNELS, toChannel } from "@/lib/channels"

interface SourceBadgesProps {
  /** Message sources in display order ("gmail", "imessage", "whatsapp"). */
  sources: readonly string[]
}

// The `source_badges` macro: a dot and the channel's name per source. The dot's colour is the
// old page's (base.css `.source-<name> i`); a source with no channel shows its own name and a
// muted dot.
export function SourceBadges({ sources }: SourceBadgesProps) {
  return (
    <>
      {sources.map((source) => {
        const channel = toChannel(source)
        return (
          <span key={source} className={`source source-${source}`}>
            <i aria-hidden="true" />
            {channel ? CHANNELS[channel].title : source}
          </span>
        )
      })}
    </>
  )
}
