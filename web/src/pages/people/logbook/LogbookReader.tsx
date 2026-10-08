import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link, useLocation, useNavigate, useOutletContext, useSearchParams } from "react-router-dom"

import { EmptyState, SearchField, SourcePill } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { isTyping, useKeys } from "@/hooks/useKeys"
import { toChannels } from "@/lib/channels"
import { plural } from "@/lib/copy"
import { HOME } from "@/lib/nav"
import { LOGBOOK } from "@/lib/people/copy"
import {
  LOGBOOK_PATH,
  openedFromPeople,
  readerScope,
  scopedEntries,
  type ReaderState,
} from "@/lib/people/logbook"
import { toggled } from "@/lib/sets"

import "../styles/logbook.css"
import { ConversationPane } from "./ConversationPane"
import { EntrySection, type Picked } from "./EntrySection"
import { entriesQuery, entryQuery } from "./queries"

// Names the scope under the title; past this many it counts them instead.
const NAMED_UP_TO = 3

/** What the People page lends the reader: its one Logbook build (PeoplePage). */
export interface ReaderContext {
  building: boolean
  build: (people: readonly string[]) => Promise<void>
}

/**
 * /people/logbook?entry=<slug>…: the saved logbooks of those entries (every saved entry when
 * none is named), read from disk. The rail (the People rail's width, under the brand) lists
 * people, groups and their conversations; the pane reads the picked one. Back (the button,
 * Escape or the browser's) returns to People as it was; opened from anywhere else, the button
 * goes to People. A new scope starts fresh.
 */
export function LogbookReader() {
  const [params] = useSearchParams()
  const scope = useMemo(() => readerScope(params), [params])
  return <Reader key={scope.join("\n")} scope={scope} />
}

function Reader({ scope }: { scope: string[] }) {
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const { building, build } = useOutletContext<ReaderContext>()
  const fromPeople = openedFromPeople(location.state)
  const allState: ReaderState = { fromPeople }
  const back = useCallback(() => {
    if (fromPeople) void navigate(-1)
    else void navigate(HOME.href)
  }, [fromPeople, navigate])

  const entries = useQuery(entriesQuery)
  const shown = useMemo(() => (entries.data ? scopedEntries(entries.data, scope) : []), [entries.data, scope])
  const selectedPath = params.get("conversation")
  const selectedSlug = selectedPath?.split("/")[0]
  const entrySlug = shown.find((entry) => entry.slug === selectedSlug)?.slug ?? shown[0]?.slug ?? ""
  const detail = useQuery({ ...entryQuery(entrySlug), enabled: !!entrySlug })
  const conversation =
    detail.data?.conversations.find((row) => row.path === selectedPath) ?? detail.data?.conversations[0]
  const open = detail.data && conversation ? { slug: detail.data.slug, conversation } : null
  const pick = ({ conversation }: Picked) => {
    const next = new URLSearchParams(params)
    next.set("conversation", conversation.path)
    setParams(next, { replace: true, state: allState })
  }
  const openEntry = shown.find((entry) => entry.slug === open?.slug) ?? null

  // The list's filter: conversation names, and the channels picked (none: every channel).
  const [text, setText] = useState("")
  const [channels, setChannels] = useState<ReadonlySet<string>>(new Set())
  const available = toChannels([...new Set(shown.flatMap((entry) => entry.channels))])

  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => heading.current?.focus({ preventScroll: true }), [])
  useKeys((event, target) => {
    if (event.key === "Escape" && !isTyping(target)) back()
  })

  const names = shown.map((entry) => entry.name)
  const scopeLine =
    scope.length && names.length <= NAMED_UP_TO ? names.join(", ") : plural(shown.length, "logbook")

  let pane
  if (entries.error) {
    pane = (
      <EmptyState>
        {entries.error.message}{" "}
        <Button variant="ghost" size="sm" onClick={() => void entries.refetch()}>
          {LOGBOOK.retry}
        </Button>
      </EmptyState>
    )
  } else if (!entries.data || (shown.length && !open)) {
    pane = (
      <div className="logbook-row logbook-loading" aria-busy="true">
        <Skeleton className="h-3 w-[30%]" />
        <Skeleton className="h-3 w-[60%]" />
      </div>
    )
  } else if (open && openEntry) {
    const parent = openEntry.parent_id
    pane = (
      <ConversationPane
        key={`${open.slug}/${open.conversation.path}`}
        entry={openEntry}
        conversation={open.conversation}
        building={building}
        onRefresh={parent ? () => void build([parent]) : null}
      />
    )
  } else {
    pane = <EmptyState>{scope.length ? LOGBOOK.gone : LOGBOOK.none}</EmptyState>
  }

  return (
    <section className="logbook" data-logbook aria-labelledby="logbook-title">
      <aside className="logbook-rail">
        <div className="logbook-rail-head">
          <Button variant="ghost" size="sm" className="logbook-back" data-back onClick={back}>
            {LOGBOOK.back}
          </Button>
          {scope.length ? (
            <Link className="logbook-all" to={LOGBOOK_PATH} replace state={allState}>
              {LOGBOOK.all}
            </Link>
          ) : null}
        </div>
        <h1 id="logbook-title" className="logbook-title" ref={heading} tabIndex={-1}>
          {LOGBOOK.title}
          {entries.data ? <span className="logbook-scope">{scopeLine}</span> : null}
        </h1>
        {shown.length ? (
          <div className="logbook-filter">
            <SearchField
              className="logbook-search"
              value={text}
              placeholder={LOGBOOK.filter}
              aria-label={LOGBOOK.filter}
              onChange={(event) => setText(event.target.value)}
            />
            {available.length > 1 ? (
              <div className="logbook-channels" role="group" aria-label="Channels">
                {available.map((channel) => (
                  <button
                    key={channel}
                    type="button"
                    className="logbook-channel"
                    aria-pressed={channels.has(channel)}
                    onClick={() => setChannels(toggled(channels, channel))}
                  >
                    <SourcePill channel={channel} size="md" />
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        <nav className="logbook-list" aria-label="Saved logbooks">
          {shown.map((entry, position) => (
            <EntrySection
              key={entry.slug}
              entry={entry}
              initiallyOpen={scope.length > 0 || entry.slug === entrySlug || position === 0}
              selected={open}
              text={text}
              channels={channels}
              onPick={pick}
            />
          ))}
        </nav>
      </aside>
      <div className="logbook-main">{pane}</div>
    </section>
  )
}
