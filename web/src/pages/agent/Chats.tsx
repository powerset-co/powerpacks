import { useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { useMatch, useNavigate } from "react-router-dom"

import { CLOSE_MARK } from "@/components/shared/icons/actions"
import { SearchesIcon } from "@/components/shared/icons/nav"
import { groupThreads } from "@/lib/agent/groups"
import { newChat, openChat, openSearch, useAgent } from "@/lib/agent/store"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { THREADS_KEY, useThreads } from "@/lib/agent/useThreads"
import { archiveThread } from "@/lib/api/codex"
import { useFeatures } from "@/lib/features"
import { AGENT } from "@/lib/nav"
import { runDate } from "@/lib/searches/copy"
import { readStored } from "@/lib/storage"
import { cn } from "@/lib/utils"
import { useCatalog } from "@/pages/searches/hooks/useCatalog"

import { useSharedSearches } from "./collaboration/useCollaboration"
import { ComposeIcon } from "./icons"

/** The side nav's chats once Codex is signed in (the Searches page's own gate), headed Searches:
 *  New chat, the past chats by date and the saved searches; picking one opens it on Searches. The
 *  heading is not a link. The rail keeps New chat. */
export function Chats({ rail }: { rail: boolean }) {
  const codex = useCodexAccount()
  if (!codex.status?.installed || !codex.status.account) return null
  return <ChatList rail={rail} />
}

function ChatList({ rail }: { rail: boolean }) {
  const agent = useAgent()
  const client = useQueryClient()
  const navigate = useNavigate()
  const here = useMatch(AGENT.href) !== null
  const features = useFeatures()
  const threads = useThreads()
  const searches = useCatalog()
  const shared = useSharedSearches(features.sets)

  // A chat appears once its first turn starts, and moves up when a turn ends.
  useEffect(() => {
    void client.invalidateQueries({ queryKey: THREADS_KEY })
  }, [agent.threadId, agent.running, client])

  // The chat each saved search ran in (AgentPage writes it): that chat is the search.
  const linked = new Map<string, string>()
  for (const card of searches.data ?? []) {
    const threadId = readStored("local", `search-thread:${card.run_id}`, (raw) =>
      typeof raw === "string" ? raw : null,
    )
    if (threadId && threads.data?.some((thread) => thread.id === threadId)) linked.set(threadId, card.title)
  }
  const unlinked = (searches.data ?? []).filter((card) => ![...linked.values()].includes(card.title))

  const pick = (action: () => void) => {
    action()
    void navigate(AGENT.href)
  }
  const remove = (threadId: string) => {
    if (threadId === agent.threadId) newChat()
    void archiveThread(threadId).finally(() => client.invalidateQueries({ queryKey: THREADS_KEY }))
  }

  if (rail) {
    return (
      <div className="border-t border-line p-2">
        <button
          type="button"
          aria-label="New chat"
          title="New chat"
          onClick={() => pick(newChat)}
          className="grid h-9 w-full cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-primary transition-colors duration-fast ease-out hover:bg-secondary"
        >
          <ComposeIcon className="size-[18px]" />
        </button>
      </div>
    )
  }
  return (
    <section aria-label="Searches" className="flex min-h-0 flex-1 flex-col border-t border-line">
      <div className="p-2">
        <h2 className="m-0 flex h-9 items-center gap-2.5 px-2.5 text-[13px] font-semibold text-muted-foreground">
          <SearchesIcon className="size-[18px] shrink-0" />
          Searches
        </h2>
        <button
          type="button"
          onClick={() => pick(newChat)}
          className="flex h-9 w-full cursor-pointer items-center gap-2.5 rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 text-left text-[13px] font-medium text-foreground transition-colors duration-fast ease-out hover:bg-secondary"
        >
          <ComposeIcon className="size-[18px] shrink-0 text-primary" />
          New chat
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {threads.data?.length === 0 && (
          <p className="m-0 px-2.5 py-2 text-xs text-faint">Your chats will show up here.</p>
        )}
        {groupThreads(threads.data ?? [], new Date()).map((group) => (
          <section key={group.label} className="mb-2">
            <h2 className="m-0 px-2.5 pb-1 pt-3 text-[11px] font-semibold text-faint">{group.label}</h2>
            <ul className="m-0 flex list-none flex-col gap-px p-0">
              {group.threads.map((thread) => (
                <li key={thread.id} className="group relative">
                  <button
                    type="button"
                    aria-current={here && thread.id === agent.threadId ? "page" : undefined}
                    onClick={() => pick(() => void openChat(thread.id))}
                    className={cn(
                      "block w-full cursor-pointer truncate rounded-[var(--radius-s)] border-0 bg-transparent py-1.5 pl-2.5 pr-8 text-left text-[13px] text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground",
                      "aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground",
                    )}
                  >
                    {linked.get(thread.id) ?? thread.title}
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
        {shared
          .filter(
            (item, index) =>
              !searches.data?.some((card) => card.run_id === item.search_id) &&
              shared.findIndex((other) => other.search_id === item.search_id) === index,
          )
          .map((item) => (
            <button
              key={item.search_id}
              type="button"
              onClick={() => pick(() => openSearch(item.search_id))}
              className="block w-full truncate rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-2 text-left text-[13px] hover:bg-secondary"
            >
              {item.title}
            </button>
          ))}
        {unlinked.length ? (
          <section className="mb-2">
            <h2 className="m-0 px-2.5 pb-1 pt-3 text-[11px] font-semibold text-faint">Saved searches</h2>
            <ul className="m-0 flex list-none flex-col gap-px p-0">
              {unlinked.map((card) => (
                <li key={card.run_id}>
                  <button
                    type="button"
                    aria-current={here && card.run_id === agent.searchRun ? "page" : undefined}
                    onClick={() => pick(() => openSearch(card.run_id))}
                    className={cn(
                      "flex w-full cursor-pointer flex-col rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-1.5 text-left transition-colors duration-fast ease-out hover:bg-secondary",
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
      </div>
    </section>
  )
}
