import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"

import { EmptyState, PowersetMark, Spinner } from "@/components/shared"
import { answer, newChat, send, setFullAccess, stop, useAgent } from "@/lib/agent/store"
import { chatRun } from "@/lib/agent/searchRun"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { useThreads } from "@/lib/agent/useThreads"
import { isDesktop } from "@/lib/desktop"
import { cn } from "@/lib/utils"
import { useCatalog } from "@/pages/searches/hooks/useCatalog"
import { RunColumn } from "@/pages/searches/RunColumn"

import { ApprovalCard } from "./ApprovalCard"
import { ChatSidebar } from "./ChatSidebar"
import { CodexGate } from "./CodexGate"
import { Composer } from "./Composer"
import { ComposeIcon, SidebarIcon } from "./icons"
import { Transcript } from "./Transcript"

// Within this distance of the bottom, new output keeps the transcript pinned to the end.
const STICK_PX = 120

const STARTERS = [
  { title: "Find people", prompt: "Who in my network has founded a developer tools company?" },
  { title: "Catch up", prompt: "Who are the most interesting people I talked to this month?" },
  { title: "Find a company's people", prompt: "Who do I know at Stripe?" },
  { title: "Find an intro", prompt: "Who can introduce me to a partner at a seed fund?" },
] as const

function Welcome() {
  return (
    <div className="rise-in flex flex-col items-center gap-7 pt-[14vh] text-center max-[680px]:pt-[6vh]">
      <PowersetMark className="size-12 rounded-[12px] shadow-[var(--shadow-2)]" />
      <div className="flex flex-col gap-1.5">
        <h1 className="m-0 text-[22px] font-semibold tracking-[-.01em]">Who are you looking for?</h1>
        <p className="m-0 text-muted-foreground">
          Ask about people, companies and intros across your network.
        </p>
      </div>
      <ul
        className="m-0 grid w-full list-none grid-cols-2 gap-2.5 p-0 max-[680px]:grid-cols-1"
        aria-label="Suggestions"
      >
        {STARTERS.map(({ title, prompt }) => (
          <li key={title}>
            <button
              type="button"
              onClick={() => void send(prompt)}
              className="flex h-full w-full cursor-pointer flex-col gap-1 rounded-[var(--radius-m)] border border-line bg-card px-3.5 py-3 text-left transition-[border-color,background-color] duration-fast ease-out hover:border-line-strong hover:bg-surface-2"
            >
              <span className="text-[13px] font-semibold">{title}</span>
              <span className="text-xs text-muted-foreground">{prompt}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ChatHeader({ onToggle, runTitle }: { onToggle: () => void; runTitle: string | null }) {
  const agent = useAgent()
  const threads = useThreads()
  const title = runTitle ?? threads.data?.find(({ id }) => id === agent.threadId)?.title ?? "New chat"
  return (
    <header className="flex h-12 items-center gap-2 px-4">
      <button
        type="button"
        aria-label="Chats"
        onClick={onToggle}
        className="grid size-8 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent text-muted-foreground hover:bg-secondary hover:text-foreground min-[861px]:hidden"
      >
        <SidebarIcon className="size-4" />
      </button>
      <h2 className="m-0 min-w-0 flex-1 truncate text-[13px] font-semibold text-muted-foreground">{title}</h2>
      <button
        type="button"
        aria-label="New chat"
        onClick={newChat}
        className="grid size-8 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent text-muted-foreground hover:bg-secondary hover:text-foreground min-[861px]:hidden"
      >
        <ComposeIcon className="size-4" />
      </button>
    </header>
  )
}

function Conversation({ onToggle, runTitle }: { onToggle: () => void; runTitle: string | null }) {
  const agent = useAgent()
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const waiting = agent.running && agent.approvals.length === 0 && agent.entries.at(-1)?.kind !== "agent"

  useEffect(() => {
    const element = scroller.current
    if (element && pinned.current) element.scrollTop = element.scrollHeight
  }, [agent.entries, agent.approvals, waiting])

  return (
    <section className="grid min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <ChatHeader onToggle={onToggle} runTitle={runTitle} />
      <div
        ref={scroller}
        className="overflow-y-auto"
        onScroll={(event) => {
          const element = event.currentTarget
          pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < STICK_PX
        }}
      >
        <div
          className="mx-auto flex max-w-[760px] flex-col gap-5 px-5 pb-10 pt-2 max-[680px]:px-3"
          aria-live="polite"
        >
          {agent.loading ? (
            <EmptyState>Loading chat…</EmptyState>
          ) : agent.entries.length === 0 ? (
            <Welcome />
          ) : (
            <Transcript entries={agent.entries} />
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
        onFullAccess={(on) => void setFullAccess(on)}
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
  if (!codex.status?.installed || !codex.status.account) {
    return (
      <main className="overflow-y-auto px-5">
        <CodexGate codex={codex} />
      </main>
    )
  }
  return <Workspace />
}

/** The chats, the open chat, and the search it ran beside it (a quick search is one pond). */
function Workspace() {
  const agent = useAgent()
  const catalog = useCatalog()
  const client = useQueryClient()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const listed = useMemo(() => new Set(catalog.data?.map(({ run_id }) => run_id)), [catalog.data])
  const runId = chatRun(agent.entries, listed) ?? agent.searchRun
  const runTitle = catalog.data?.find((card) => card.run_id === runId)?.title ?? null

  // A search saves when its turn ends; look for it then.
  useEffect(() => {
    if (!agent.running) void client.invalidateQueries({ queryKey: ["searches", "catalog"] })
  }, [agent.running, client])

  return (
    <main
      className={cn(
        "grid min-h-0 grid-cols-[260px_minmax(0,1fr)] max-[860px]:grid-cols-1",
        runId && "grid-cols-[260px_minmax(340px,420px)_minmax(0,1fr)]",
      )}
    >
      <ChatSidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close chats"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-30 cursor-default border-0 bg-black/40 min-[861px]:hidden"
        />
      )}
      <Conversation onToggle={() => setSidebarOpen((open) => !open)} runTitle={runTitle} />
      {runId && (
        <div className="searches-main border-l border-line" aria-label="Search results">
          <RunColumn runId={runId} />
        </div>
      )}
    </main>
  )
}
