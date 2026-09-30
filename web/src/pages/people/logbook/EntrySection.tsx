import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { Fold, SourcePill, SourcePills } from "@/components/shared"
import { Skeleton } from "@/components/ui/skeleton"
import type { LogbookConversation, LogbookEntry } from "@/lib/api/logbook"
import { toChannel, toChannels } from "@/lib/channels"
import { conversationTitle, entryMeta, filterConversations, span } from "@/lib/people/logbook"

import { entryQuery } from "./queries"

export interface Picked {
  slug: string
  conversation: LogbookConversation
}

interface EntrySectionProps {
  entry: LogbookEntry
  initiallyOpen: boolean
  selected: Picked | null
  // The rail's filter (LogbookReader).
  text: string
  channels: ReadonlySet<string>
  onPick: (picked: Picked) => void
}

// One saved person or group in the reader's rail; its conversations load when it first opens.
export function EntrySection({ entry, initiallyOpen, selected, text, channels, onPick }: EntrySectionProps) {
  const [open, setOpen] = useState(initiallyOpen)
  const detail = useQuery({ ...entryQuery(entry.slug), enabled: open })
  const all = detail.data?.conversations ?? []
  const shown = filterConversations(all, text, channels)
  const filtered = shown.length !== all.length
  return (
    <section className="logbook-entry" data-entry={entry.slug}>
      <h2>
        <button
          type="button"
          className="logbook-entry-toggle chevron"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <span className="logbook-entry-name">{entry.name}</span>
          <span className="logbook-entry-meta">
            {filtered && detail.data
              ? `${shown.length.toLocaleString()} of ${all.length.toLocaleString()} conversations`
              : entryMeta(entry)}
          </span>
          <SourcePills className="logbook-entry-pills" channels={toChannels(entry.channels)} />
        </button>
      </h2>
      <Fold open={open}>
        {detail.data ? (
          <ul className="logbook-convs">
            {shown.map((conversation) => (
              <li key={conversation.path}>
                <ConversationButton
                  conversation={conversation}
                  current={selected?.slug === entry.slug && selected.conversation.path === conversation.path}
                  onClick={() => onPick({ slug: entry.slug, conversation })}
                />
              </li>
            ))}
            {!shown.length ? <li className="logbook-note">No conversations match.</li> : null}
          </ul>
        ) : detail.error ? (
          <p className="logbook-note">{detail.error.message}</p>
        ) : (
          <div className="logbook-note" aria-busy="true">
            <Skeleton className="h-3 w-[70%]" />
          </div>
        )}
      </Fold>
    </section>
  )
}

interface ConversationButtonProps {
  conversation: LogbookConversation
  current: boolean
  onClick: () => void
}

function ConversationButton({ conversation, current, onClick }: ConversationButtonProps) {
  const channel = toChannel(conversation.channel)
  return (
    <button
      type="button"
      className="logbook-conv focus-bar"
      aria-current={current || undefined}
      data-focus={current}
      onClick={onClick}
    >
      <span className="logbook-conv-pill">{channel ? <SourcePill channel={channel} /> : null}</span>
      <span className="logbook-conv-title">{conversationTitle(conversation)}</span>
      <span className="logbook-conv-count">{conversation.messages.toLocaleString()}</span>
      <span className="logbook-conv-when">{span(conversation.first_at, conversation.last_at)}</span>
    </button>
  )
}
