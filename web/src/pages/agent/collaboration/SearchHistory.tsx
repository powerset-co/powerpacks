import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react"

import { Appear, Avatar, Chip, EmptyState, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { SharedMessage } from "@/lib/api/collaboration"
import { readStored, writeStored } from "@/lib/storage"
import { isDesktop } from "@/lib/desktop"

import { Composer, type ComposerProps } from "../Composer"
import { Markdown } from "../Markdown"
import type { useCollaboration } from "./useCollaboration"

type Collaboration = ReturnType<typeof useCollaboration>
interface Row {
  id: string
  message?: SharedMessage
}
interface Props {
  searchId: string
  title: string
  collaboration: Collaboration
  children?: ReactNode
  composer?: Pick<ComposerProps, "running" | "fullAccess" | "onFullAccess" | "onStop">
  onPrivateSend?: (text: string) => Promise<void>
}

/** Questions belong to a search; the set selects the recipients, never a second chat workspace. */
export function SearchHistory({
  searchId,
  title,
  collaboration: c,
  children,
  composer,
  onPrivateSend,
}: Props) {
  const conversations = useMemo(
    () => c.data?.conversations.filter((item) => item.search_id === searchId) ?? [],
    [c.data?.conversations, searchId],
  )
  const [chosenSet, updateSet] = useState(
    () =>
      readStored("session", `search-set:${searchId}`, (value) =>
        typeof value === "string" ? value : null,
      ) ?? "",
  )
  const setChosenSet = (value: string) => {
    updateSet(value)
    writeStored("session", `search-set:${searchId}`, value)
  }
  const set =
    c.data?.sets.find((item) => item.set_id === (chosenSet || conversations[0]?.set_id)) ?? c.data?.sets[0]
  const messages = useMemo(
    () =>
      conversations.flatMap((item) => item.messages).sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [conversations],
  )
  const members = set?.members ?? []
  const [recipient, updateRecipient] = useState(
    () =>
      readStored("session", `search-recipient:${searchId}`, (value) =>
        typeof value === "string" ? value : null,
      ) ?? "",
  )
  const setRecipient = (value: string) => {
    updateRecipient(value)
    writeStored("session", `search-recipient:${searchId}`, value)
  }
  const draftKey = `search-draft:${searchId}`
  const [draft, updateDraft] = useState(
    () => readStored("session", draftKey, (value) => (typeof value === "string" ? value : null)) ?? "",
  )
  const setDraft = (text: string) => {
    updateDraft(text)
    writeStored("session", draftKey, text)
  }
  const [picking, setPicking] = useState(false)
  const [replyId, updateReply] = useState(
    () =>
      readStored("session", `search-reply:${searchId}`, (value) =>
        typeof value === "string" ? value : null,
      ) ?? "",
  )
  const replyTo = messages.find((message) => message.id === replyId) ?? null
  const setReplyTo = (message: SharedMessage | null) => {
    updateReply(message?.id ?? "")
    writeStored("session", `search-reply:${searchId}`, message?.id ?? "")
  }
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [sendError, setSendError] = useState("")
  const scroll = useRef<VirtualRowsHandle>(null)
  const following = useRef(true)
  const [atBottom, setAtBottom] = useState(true)
  const selected = members.find((member) => member.operator_id === recipient)
  const mention = /(?:^|\s)@([^@\n]*)$/.exec(draft)?.[1]
  const people = (c.data?.sets ?? [])
    .flatMap((group) => group.members.map((member) => ({ ...member, set: group })))
    .filter(
      (member) =>
        member.operator_id !== c.data?.me.operator_id &&
        (mention === undefined || member.name.toLowerCase().includes(mention.toLowerCase())),
    )
  const rows = useMemo<Row[]>(() => {
    const replies = new Map<string, SharedMessage[]>()
    for (const message of messages) {
      if (message.reply_to) replies.set(message.reply_to, [...(replies.get(message.reply_to) ?? []), message])
    }
    return [
      ...(children ? [{ id: "private-history" }] : []),
      ...messages
        .filter((message) => !message.reply_to)
        .flatMap((message) => [
          { id: message.id, message },
          ...(expanded.has(message.id)
            ? (replies.get(message.id) ?? []).map((reply) => ({ id: reply.id, message: reply }))
            : []),
        ]),
    ]
  }, [children, messages, expanded])

  useLayoutEffect(() => {
    if (following.current && rows.length) scroll.current?.scrollToIndex(rows.length - 1, { align: "end" })
  }, [rows])

  const send = async (text: string) => {
    setSendError("")
    const to = replyTo?.recipient_id ?? recipient
    if (!to) {
      if (text.includes("@")) {
        setSendError("Choose a member from the @ menu before sending.")
        return false
      }
      if (onPrivateSend) {
        await onPrivateSend(text)
        return true
      }
      setSendError("Mention a member to ask about this search.")
      return false
    }
    if (!set) return false
    const conversation = conversations.find((item) =>
      replyTo ? item.messages.some((message) => message.id === replyTo.id) : item.set_id === set.set_id,
    )
    const saved = conversation ?? (await c.create(set.set_id, searchId, title))
    if (!saved) return false
    const question = text.trim()
    if (!question) return false
    const sent = await c.send(saved.id, question, to, replyTo?.id)
    if (!sent) return false
    if (replyTo) setExpanded((old) => new Set(old).add(replyTo.id))
    setRecipient("")
    setPicking(false)
    return true
  }

  const renderMessage = (message: SharedMessage) => {
    const conversation = conversations.find((item) => item.messages.some((held) => held.id === message.id))
    const group = c.data?.sets.find((item) => item.set_id === conversation?.set_id)
    const owner = group?.members.find((member) => member.operator_id === message.recipient_id)
    const pending = c.pending.find((item) => item.id === message.request_id)
    const replies = messages.filter((item) => item.reply_to === message.id)
    return (
      <article
        data-message-id={message.id}
        className={`mx-auto max-w-[760px] px-5 py-3 ${message.reply_to ? "pl-12" : ""}`}
      >
        <div className="flex items-center gap-2 text-xs">
          <Avatar name={message.author_name} />
          <strong>{message.author_name}</strong>
          <span className="text-faint">{group?.name}</span>
        </div>
        {owner && <p className="mb-1 mt-2 text-xs text-info">@{owner.name}</p>}
        <p className="m-0 whitespace-pre-wrap break-words">{message.text}</p>
        {message.request_id && (
          <div className="mt-3 border-l-2 border-line pl-3 text-[13px]">
            <p className="mb-2 mt-0 text-xs font-semibold">
              {owner?.name}’s assistant{" "}
              <span className="ml-1 font-normal text-faint">
                {message.status === "answered"
                  ? "replied"
                  : message.status === "working"
                    ? "working…"
                    : message.status === "failed"
                      ? "could not answer"
                      : message.status === "unsent"
                        ? "not delivered"
                        : "waiting for recipient"}
              </span>
            </p>
            {message.answer ? (
              <Markdown text={message.answer} />
            ) : message.error ? (
              <p className="text-bad">{message.error}</p>
            ) : null}
            {pending && (
              <Button
                size="sm"
                disabled={!isDesktop() || c.answering !== null}
                onClick={() => void c.answer(pending)}
              >
                {!isDesktop()
                  ? "Open desktop app to answer"
                  : c.answering === pending.id
                    ? "Working…"
                    : pending.answer || pending.error
                      ? "Retry reply delivery"
                      : pending.thread_id
                        ? "Recover saved session"
                        : "Answer with my Codex"}
              </Button>
            )}
            {message.status === "unsent" && (
              <Button
                size="sm"
                disabled={c.busy}
                onClick={() => conversation && void c.resend(conversation.id, message.id)}
              >
                Retry delivery
              </Button>
            )}
          </div>
        )}
        {!message.reply_to && message.request_id && (
          <div className="mt-2 flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setReplyTo(message)
                setRecipient("")
                setExpanded((old) => new Set(old).add(message.id))
              }}
            >
              Reply in thread
            </Button>
            {!!replies.length && (
              <Button
                variant="ghost"
                size="sm"
                aria-expanded={expanded.has(message.id)}
                onClick={() =>
                  setExpanded((old) => {
                    const next = new Set(old)
                    if (next.has(message.id)) next.delete(message.id)
                    else next.add(message.id)
                    return next
                  })
                }
              >
                {expanded.has(message.id) ? "Hide" : "Show"} {replies.length} follow-up
                {replies.length === 1 ? "" : "s"}
              </Button>
            )}
          </div>
        )}
      </article>
    )
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {rows.length ? (
        <VirtualRows
          aria-label="Search history"
          items={rows}
          getKey={(row) => row.id}
          rowHeight={180}
          measure
          overscan={5}
          handle={scroll}
          className="min-h-0 flex-1 overflow-y-auto [overflow-anchor:none]"
          onScroll={(event) => {
            const node = event.currentTarget
            following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 100
            setAtBottom(following.current)
          }}
          renderRow={(row) =>
            row.message ? (
              renderMessage(row.message)
            ) : (
              <div className="mx-auto flex max-w-[760px] flex-col gap-5 px-5 pb-5 pt-2">{children}</div>
            )
          }
        />
      ) : (
        <EmptyState>Mention someone to ask about this search. Answers stay under your question.</EmptyState>
      )}
      <div className="pointer-events-none absolute bottom-36 left-0 right-0 flex justify-center">
        <Appear show={!atBottom}>
          <Button
            className="pointer-events-auto shadow-2"
            shape="pill"
            onClick={() => {
              following.current = true
              setAtBottom(true)
              scroll.current?.scrollToIndex(rows.length - 1, { align: "end" })
            }}
          >
            Jump to latest
          </Button>
        </Appear>
      </div>
      <div className="relative shrink-0 pt-2">
        {(c.error ?? sendError) && (
          <p role="alert" className="mx-5 my-2 text-xs text-bad">
            {sendError.length ? sendError : c.error}
          </p>
        )}
        {replyTo && (
          <div className="mx-5 mb-2 flex items-center gap-2 text-xs">
            <span className="min-w-0 flex-1 truncate text-faint">Replying to: {replyTo.text}</span>
            <Button size="sm" variant="ghost" onClick={() => setReplyTo(null)}>
              Leave thread
            </Button>
          </div>
        )}
        <Appear
          show={picking && !replyTo}
          className="absolute bottom-full left-5 z-10 !flex-col !items-stretch rounded-[var(--radius-m)] border border-line bg-card p-1 shadow-2"
        >
          <p className="m-0 px-3 py-2 text-xs text-faint">Ask a member’s assistant</p>
          <VirtualRows
            items={people}
            getKey={(member) => `${member.set.set_id}:${member.operator_id}`}
            rowHeight={44}
            overscan={3}
            className="w-64 overflow-y-auto"
            style={{ height: Math.min(people.length, 6) * 44 }}
            renderRow={(member) => (
              <Button
                variant="ghost"
                className="h-11 w-full justify-start"
                onClick={() => {
                  setChosenSet(member.set.set_id)
                  setRecipient(member.operator_id)
                  setDraft(draft.replace(/(?:^|\s)@[^@\n]*$/, "").trimEnd())
                  setPicking(false)
                }}
              >
                <Avatar name={member.name} />
                <span className="min-w-0 truncate">{member.name}</span>
                <span className="ml-auto truncate text-xs text-faint">{member.set.name}</span>
              </Button>
            )}
          />
          {!people.length && <p className="px-3 text-xs text-faint">No matching members</p>}
        </Appear>
        <Composer
          {...composer}
          running={composer?.running ?? false}
          disabled={c.busy}
          draft={draft}
          onDraftChange={(text) => {
            setDraft(text)
            setPicking(/(?:^|\s)@[^@\n]*$/.test(text))
          }}
          onSend={send}
          placeholder={
            replyTo
              ? "Follow up in this thread…"
              : selected
                ? `Ask ${selected.name}…`
                : onPrivateSend
                  ? "Ask Codex, or @mention someone…"
                  : "@mention someone about this search…"
          }
          footer={
            <div className="flex min-w-0 items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                aria-label="Mention a member"
                aria-expanded={picking}
                disabled={!set || !!replyTo}
                onClick={() => setPicking(!picking)}
              >
                {" "}
                @{" "}
              </Button>
              {selected ? (
                <Chip pressed removable onClick={() => setRecipient("")}>
                  {selected.name} · {set?.name}
                </Chip>
              ) : (
                <span className="truncate text-[11px] text-faint">
                  {replyTo ? "Shared with this set" : "Only @questions are shared"}
                </span>
              )}
              {composer?.onFullAccess && !selected && !replyTo && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={composer.fullAccess}
                  onClick={() => composer.onFullAccess?.(!composer.fullAccess)}
                >
                  Full access
                </Button>
              )}
            </div>
          }
        />
      </div>
    </div>
  )
}
