import { useQuery } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"

import { EmptyState, SourcePill, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { LogbookConversation, LogbookEntry } from "@/lib/api/logbook"
import { toChannel } from "@/lib/channels"
import { LOGBOOK } from "@/lib/people/copy"
import {
  conversationMeta,
  conversationTitle,
  readerItems,
  readerMonths,
  type ReaderItem,
} from "@/lib/people/logbook"

import { People } from "./People"
import { conversationQuery } from "./queries"
import { Timeline } from "./Timeline"

// A message row's height before it is measured.
const ROW_ESTIMATE = 56

const itemKey = (item: ReaderItem) => item.key

interface ConversationPaneProps {
  // Whose logbook this is: the person or group the conversation belongs to.
  entry: LogbookEntry
  conversation: LogbookConversation
  building: boolean
  // Rebuilds this person's logbook; null for a group, which is built with its people.
  onRefresh: (() => void) | null
}

/**
 * The picked conversation, keyed by the reader so a new pick never shows the last one's body:
 * its identity (whose logbook, which channel and thread) at once, the messages when they load.
 * Oldest message first, only the rows on screen mounted; the timeline jumps by month and follows
 * the scroll. Message text is rendered as text: the archive is untrusted.
 */
export function ConversationPane({ entry, conversation, building, onRefresh }: ConversationPaneProps) {
  const query = useQuery(conversationQuery(entry.slug, conversation.path))
  const items = useMemo(() => (query.data ? readerItems(query.data.messages) : []), [query.data])
  const months = useMemo(() => readerMonths(items), [items])
  const rows = useRef<VirtualRowsHandle>(null)
  const [month, setMonth] = useState<string | null>(null)
  const frame = useRef(0)
  const channel = toChannel(conversation.channel)

  useEffect(() => () => cancelAnimationFrame(frame.current), [])

  const jump = (index: number, key: string) => {
    rows.current?.scrollToIndex(index, { align: "start" })
    setMonth(key)
  }
  // The timeline's month follows the first row in view, read once per frame.
  const follow = () => {
    cancelAnimationFrame(frame.current)
    frame.current = requestAnimationFrame(() => {
      const index = rows.current?.firstVisible() ?? 0
      setMonth(items[index]?.month ?? null)
    })
  }

  return (
    <article className="logbook-pane" data-conversation={conversation.path}>
      <header className="logbook-pane-head">
        <p className="logbook-eyebrow">
          <span className="logbook-eyebrow-name">{entry.name}</span>
          <span>{entry.kind === "group" ? "Group" : "Person"}</span>
        </p>
        <div className="logbook-pane-title">
          {channel ? <SourcePill channel={channel} size="md" /> : null}
          <h2>{conversationTitle(conversation)}</h2>
        </div>
        <p className="logbook-pane-meta">{conversationMeta(conversation)}</p>
        {query.data ? <People conversation={conversation} body={query.data} /> : null}
        {onRefresh ? (
          <Button
            variant="ghost"
            size="sm"
            className="logbook-refresh"
            title={LOGBOOK.explainer}
            disabled={building}
            onClick={onRefresh}
          >
            {building ? LOGBOOK.refreshing : LOGBOOK.refresh}
          </Button>
        ) : null}
      </header>
      {query.data ? (
        items.length ? (
          <div className="logbook-read rise-in">
            <VirtualRows
              handle={rows}
              className="logbook-viewport"
              data-messages
              items={items}
              rowHeight={ROW_ESTIMATE}
              measure
              overscan={8}
              getKey={itemKey}
              renderRow={(item) => <Row item={item} />}
              onScroll={follow}
            />
            {months.length > 1 ? (
              <Timeline
                months={months}
                active={month ?? months[0]?.key ?? ""}
                onJump={(picked) => jump(picked.index, picked.key)}
                onOldest={() => jump(0, months[0]?.key ?? "")}
                onNewest={() => {
                  rows.current?.scrollToIndex(items.length - 1, { align: "end" })
                  setMonth(months.at(-1)?.key ?? null)
                }}
              />
            ) : null}
          </div>
        ) : (
          <EmptyState>{LOGBOOK.empty}</EmptyState>
        )
      ) : query.error ? (
        <EmptyState>{query.error.message}</EmptyState>
      ) : (
        <div className="logbook-row logbook-loading" aria-busy="true" aria-label="Loading messages">
          <Skeleton className="h-3 w-[40%]" />
          <Skeleton className="h-3 w-[75%]" />
          <Skeleton className="h-3 w-[60%]" />
        </div>
      )}
    </article>
  )
}

function Row({ item }: { item: ReaderItem }) {
  if (item.kind === "day") {
    return (
      <div className="logbook-row">
        <p className="logbook-day">{item.label}</p>
      </div>
    )
  }
  return (
    <div className="logbook-row logbook-msg" data-first={item.sender !== null}>
      <span className="logbook-msg-time">{item.time}</span>
      <div className="logbook-msg-body">
        {item.sender !== null ? <b className="logbook-msg-sender">{item.sender}</b> : null}
        {item.message.text ? (
          <p className="logbook-msg-text">{item.message.text}</p>
        ) : (
          <p className="logbook-msg-text logbook-msg-none">No text</p>
        )}
      </div>
    </div>
  )
}
