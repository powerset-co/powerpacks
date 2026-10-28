import { useFeatures } from "@/lib/features"
import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useMemo, useRef } from "react"

import { EmptyState, PowersetMark, Spinner } from "@/components/shared"
import { useFullAccessTip } from "@/lib/agent/fullAccessTip"
import { answer, send, setFullAccess, stop, useAgent } from "@/lib/agent/store"
import { chatRun } from "@/lib/agent/searchRun"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { useSearchScope } from "@/lib/agent/useSearchScope"
import { useThreads } from "@/lib/agent/useThreads"
import { isDesktop } from "@/lib/desktop"
import { collapseSidebar } from "@/lib/sidebar"
import { writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"
import { useCatalog } from "@/pages/searches/hooks/useCatalog"
import { searchRunKey } from "@/pages/searches/hooks/useSearchRun"
import { RunColumn } from "@/pages/searches/RunColumn"

import { ApprovalCard } from "./ApprovalCard"
import { CodexGate } from "./CodexGate"
import { Composer } from "./Composer"
import { FullAccessTip } from "./FullAccessTip"
import { ModelPicker } from "./ModelPicker"
import { SearchScope } from "./SearchScope"
import { Transcript } from "./Transcript"
import { SearchHistory } from "./collaboration/SearchHistory"
import { useCollaboration } from "./collaboration/useCollaboration"

// Within this distance of the bottom, new output keeps the transcript pinned to the end.
const STICK_PX = 120

function Welcome() {
  return (
    <div className="rise-in flex flex-col items-center gap-7 pt-[14vh] text-center max-[680px]:pt-[6vh]">
      <PowersetMark className="size-12 rounded-[12px] shadow-[var(--shadow-2)]" />
      <div className="flex flex-col gap-1.5">
        <h1 className="m-0 text-[20px] font-semibold tracking-[-.01em]">Who are you looking for?</h1>
        <p className="m-0 text-muted-foreground">
          Ask about people, companies and intros across your network.
        </p>
      </div>
    </div>
  )
}

function ChatHeader({ runTitle }: { runTitle: string | null }) {
  const agent = useAgent()
  const threads = useThreads()
  const title = runTitle ?? threads.data?.find(({ id }) => id === agent.threadId)?.title ?? "New chat"
  return (
    <header className="flex h-12 items-center px-4">
      <h2 className="m-0 min-w-0 flex-1 truncate text-[13px] font-semibold text-muted-foreground">{title}</h2>
    </header>
  )
}

function Conversation({
  runTitle,
  runId,
  collaboration,
}: {
  runTitle: string | null
  runId: string | null
  collaboration: ReturnType<typeof useCollaboration>
}) {
  const features = useFeatures()
  const agent = useAgent()
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const waiting = agent.running && agent.approvals.length === 0 && agent.entries.at(-1)?.kind !== "agent"
  const tip = useFullAccessTip()
  // Full access on retires the tip for good; the switch and the tip's button both go through here.
  const fullAccess = (on: boolean) => {
    if (on) tip.dismiss()
    void setFullAccess(on)
  }

  useEffect(() => {
    const element = scroller.current
    if (element && pinned.current) element.scrollTop = element.scrollHeight
  }, [agent.entries, agent.approvals, waiting])

  if (runId && features.sets)
    return (
      <section className="flex min-h-0 min-w-0 flex-col">
        <ChatHeader runTitle={runTitle} />
        <SearchHistory
          key={runId}
          searchId={runId}
          title={runTitle ?? "Search"}
          collaboration={collaboration}
          onPrivateSend={send}
          composer={{
            running: agent.running,
            fullAccess: agent.fullAccess,
            onFullAccess: fullAccess,
            onStop: () => void stop(),
          }}
        >
          {agent.entries.length > 0 && <Transcript entries={agent.entries} running={agent.running} />}
          {tip.shown && agent.approvals.length > 0 && !agent.fullAccess && (
            <FullAccessTip onEnable={() => fullAccess(true)} onDismiss={tip.dismiss} />
          )}
          {agent.approvals.map((approval) => (
            <ApprovalCard
              key={String(approval.requestId)}
              approval={approval}
              onAnswer={(choice) => void answer(approval, choice)}
            />
          ))}
          {waiting && (
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <Spinner />
              Working
            </p>
          )}
        </SearchHistory>
      </section>
    )

  return (
    <section className="grid min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <ChatHeader runTitle={runTitle} />
      <div
        ref={scroller}
        className="overflow-y-auto"
        onScroll={(event) => {
          const element = event.currentTarget
          pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < STICK_PX
        }}
      >
        <div
          className="mx-auto flex max-w-[680px] flex-col gap-7 px-5 pb-10 pt-2 max-[680px]:px-3"
          aria-live="polite"
        >
          {agent.loading ? (
            <EmptyState>Loading chat…</EmptyState>
          ) : agent.entries.length === 0 ? (
            <Welcome />
          ) : (
            <Transcript entries={agent.entries} running={agent.running} />
          )}
          {tip.shown && agent.approvals.length > 0 && !agent.fullAccess && (
            <FullAccessTip onEnable={() => fullAccess(true)} onDismiss={tip.dismiss} />
          )}
          {agent.approvals.map((approval) => (
            <ApprovalCard
              key={String(approval.requestId)}
              approval={approval}
              onAnswer={(choice) => void answer(approval, choice)}
            />
          ))}
          {waiting && (
            <p className="m-0 flex items-center gap-2 text-xs text-muted-foreground">
              <Spinner />
              Working
            </p>
          )}
        </div>
      </div>
      <Composer
        running={agent.running}
        fullAccess={agent.fullAccess}
        onSend={(text) => void send(text)}
        onStop={() => void stop()}
        onFullAccess={fullAccess}
        footer={
          <>
            <SearchScope />
            <ModelPicker />
          </>
        }
      />
    </section>
  )
}

/** Chats with Codex, signed in with ChatGPT, searching with Powerpacks. Desktop app only. */
export function AgentPage() {
  if (!isDesktop()) {
    return <EmptyState>Chat runs in the Powerpacks desktop app.</EmptyState>
  }
  return <SignedInAgent />
}

function SignedInAgent() {
  const codex = useCodexAccount()
  useSearchScope()
  if (!codex.status?.installed || !codex.status.account) {
    return (
      <main className="overflow-y-auto px-5">
        <CodexGate codex={codex} />
      </main>
    )
  }
  return <Workspace />
}

/** The open chat, and the search it ran beside it (a quick search is one pond); the side nav lists
 *  the chats. */
function Workspace() {
  const agent = useAgent()
  const features = useFeatures()
  const collaboration = useCollaboration(features.sets)
  const catalog = useCatalog(agent.running)
  const client = useQueryClient()
  const listed = useMemo(() => new Set(catalog.data?.map(({ run_id }) => run_id)), [catalog.data])
  const runId = chatRun(agent.entries, listed) ?? agent.searchRun
  const card = catalog.data?.find((item) => item.run_id === runId)
  const runTitle =
    card?.title ?? collaboration.data?.conversations.find((item) => item.search_id === runId)?.title ?? null
  const runRunning = card?.status === "running"
  // The run last seen running; only that same run leaving "running" is re-read.
  const lastRunning = useRef(runRunning ? runId : null)

  // A search saves when its turn ends; look for it then.
  useEffect(() => {
    if (!agent.running) void client.invalidateQueries({ queryKey: ["searches", "catalog"] })
  }, [agent.running, client])

  // A running search shows its screened people; when it stops running, read its ranked ones.
  useEffect(() => {
    if (runId && lastRunning.current === runId && !runRunning)
      void client.invalidateQueries({ queryKey: searchRunKey(runId) })
    lastRunning.current = runRunning ? runId : null
  }, [client, runId, runRunning])

  // A search run in this chat is this chat's: the side nav lists the two as one.
  useEffect(() => {
    if (runId && agent.threadId && listed.has(runId))
      writeStored("local", `search-thread:${runId}`, agent.threadId)
  }, [runId, agent.threadId, listed])

  // Results open beside the chat: the sidebar folds to its rail, once per search.
  const showing = runId !== null && listed.has(runId)
  const folded = useRef<string | null>(null)
  useEffect(() => {
    if (!showing || folded.current === runId) return
    folded.current = runId
    collapseSidebar()
  }, [showing, runId])

  // Chat and results split the room 2:3; the results keep 380px at the narrowest window (960px).
  return (
    <main
      className={cn(
        "grid min-h-0 grid-cols-[minmax(0,1fr)]",
        showing && "grid-cols-[minmax(0,2fr)_minmax(380px,3fr)]",
      )}
    >
      <Conversation runTitle={runTitle} runId={runId} collaboration={collaboration} />
      {runId && listed.has(runId) && (
        <div className="searches-main border-l border-line" aria-label="Search results">
          <RunColumn runId={runId} />
        </div>
      )}
    </main>
  )
}
