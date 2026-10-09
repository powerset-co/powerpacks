import { useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"

import { CLOSE_MARK } from "@/components/shared"
import { groupThreads } from "@/lib/agent/groups"
import { newChat, openChat, openSearch, useAgent } from "@/lib/agent/store"
import { THREADS_KEY, useThreads } from "@/lib/agent/useThreads"
import { archiveThread } from "@/lib/api/codex"
import { runDate } from "@/lib/searches/copy"
import { cn } from "@/lib/utils"
import { useCatalog } from "@/pages/searches/hooks/useCatalog"

import { ComposeIcon } from "./icons"

interface ChatSidebarProps {
  open: boolean
  onClose: () => void
}

/** New chat and the past chats by date. A column on wide windows, a drawer on narrow ones. */
export function ChatSidebar({ open, onClose }: ChatSidebarProps) {
  const agent = useAgent()
  const client = useQueryClient()
  const threads = useThreads()
  const searches = useCatalog()

  // A chat appears once its first turn starts, and moves up when a turn ends.
  useEffect(() => {
    void client.invalidateQueries({ queryKey: THREADS_KEY })
  }, [agent.threadId, agent.running, client])

  const pick = (action: () => void) => {
    action()
    onClose()
  }
  const remove = (threadId: string) => {
    if (threadId === agent.threadId) newChat()
    void archiveThread(threadId).finally(() => client.invalidateQueries({ queryKey: THREADS_KEY }))
  }

  return (
    <aside
      aria-label="Chats"
      data-open={open}
      // An overlay below 860px, and below 1100px when the page (`group/agent`) shows a run.
      className={cn(
        "z-40 flex min-h-0 w-[260px] flex-col border-r border-line bg-[color-mix(in_srgb,var(--card)_55%,var(--background))]",
        "max-[860px]:fixed max-[860px]:bottom-0 max-[860px]:left-0 max-[860px]:top-0 max-[860px]:-translate-x-full max-[860px]:shadow-[var(--shadow-2)] max-[860px]:transition-transform max-[860px]:duration-med max-[860px]:ease-out max-[860px]:data-[open=true]:translate-x-0",
        "group-data-[run]/agent:max-[1100px]:fixed group-data-[run]/agent:max-[1100px]:bottom-0 group-data-[run]/agent:max-[1100px]:left-0 group-data-[run]/agent:max-[1100px]:top-0 group-data-[run]/agent:max-[1100px]:-translate-x-full group-data-[run]/agent:max-[1100px]:shadow-[var(--shadow-2)] group-data-[run]/agent:max-[1100px]:transition-transform group-data-[run]/agent:max-[1100px]:duration-med group-data-[run]/agent:max-[1100px]:ease-out group-data-[run]/agent:max-[1100px]:data-[open=true]:translate-x-0",
      )}
    >
      <div className="p-3">
        <button
          type="button"
          onClick={() => pick(newChat)}
          className="flex w-full cursor-pointer items-center gap-2.5 rounded-[var(--radius-m)] border border-line bg-card px-3 py-2 text-[13px] font-semibold text-foreground transition-colors duration-fast ease-out hover:border-line-strong hover:bg-surface-2"
        >
          <ComposeIcon className="size-4 text-primary" />
          New chat
        </button>
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {threads.data?.length === 0 && (
          <p className="m-0 px-3 py-2 text-xs text-faint">Your chats will show up here.</p>
        )}
        {groupThreads(threads.data ?? [], new Date()).map((group) => (
          <section key={group.label} className="mb-2">
            <h2 className="m-0 px-3 pb-1 pt-3 text-[11px] font-semibold text-faint">{group.label}</h2>
            <ul className="m-0 flex list-none flex-col gap-px p-0">
              {group.threads.map((thread) => (
                <li key={thread.id} className="group relative">
                  <button
                    type="button"
                    aria-current={thread.id === agent.threadId ? "page" : undefined}
                    onClick={() => pick(() => void openChat(thread.id))}
                    className={cn(
                      "block w-full cursor-pointer truncate rounded-[var(--radius-s)] border-0 bg-transparent py-1.5 pl-3 pr-8 text-left text-[13px] text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground",
                      "aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground",
                    )}
                  >
                    {thread.title}
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete ${thread.title}`}
                    onClick={() => remove(thread.id)}
                    className="absolute right-1 top-1/2 grid size-6 -translate-y-1/2 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent text-base leading-none text-faint opacity-0 transition-opacity duration-fast hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
                  >
                    {CLOSE_MARK}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
        {searches.data?.length ? (
          <section className="mb-2">
            <h2 className="m-0 px-3 pb-1 pt-3 text-[11px] font-semibold text-faint">Searches</h2>
            <ul className="m-0 flex list-none flex-col gap-px p-0">
              {searches.data.map((card) => (
                <li key={card.run_id}>
                  <button
                    type="button"
                    aria-current={card.run_id === agent.searchRun ? "page" : undefined}
                    onClick={() => pick(() => openSearch(card.run_id))}
                    className={cn(
                      "flex w-full cursor-pointer flex-col rounded-[var(--radius-s)] border-0 bg-transparent px-3 py-1.5 text-left transition-colors duration-fast ease-out hover:bg-secondary",
                      "aria-[current=page]:bg-surface-2",
                    )}
                  >
                    <span className="truncate text-[13px] text-foreground">{card.title}</span>
                    <span className="truncate text-[11px] text-faint">
                      {[card.company, runDate(card.created_at)].filter(Boolean).join(" · ")}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </nav>
    </aside>
  )
}
