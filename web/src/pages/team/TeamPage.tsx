import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"

import { initials, Page } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { askAgent, fetchTeam, fetchThread, type SupportMessage, type Teammate } from "@/lib/api/team"
import { SETS } from "@/lib/people/copy"
import { cn } from "@/lib/utils"

import { Markdown } from "../agent/Markdown"

const TEAM_KEY = ["team"] as const
const THREAD_KEY = (operatorId: string) => ["team", "thread", operatorId] as const
// Presence moves every few seconds; a thread answer arrives when their agent finishes, minutes later.
const TEAM_POLL_MS = 10_000
const THREAD_POLL_MS = 5_000
// Their agent polls the relay every few seconds; past this it reads as away.
const AWAKE_MS = 90_000

const TEAM = {
  title: "Team",
  lead: "Everyone on Powerset with the app. Pick someone to work on their install through their agent: it reads their logs, runs commands in their Powerpacks checkout, fixes what it can, and can restart their app. Nothing of theirs leaves their computer; you get its report.",
  nobody: "No teammate's agent has checked in yet.",
  pick: "Pick a teammate.",
  placeholder: "What should their agent look into?",
  send: "Send",
  sending: "Sending…",
  empty: "No requests yet.",
  you: "You",
  agent: (name: string) => `${name}'s agent`,
  awake: "Agent up",
  away: "Away",
}

/** Teammates with their agents' presence, and one support thread at a time (?with=operator id). */
export function TeamPage() {
  const [params, setParams] = useSearchParams()
  const chosenId = params.get("with") ?? ""
  const team = useQuery({ queryKey: TEAM_KEY, queryFn: fetchTeam, refetchInterval: TEAM_POLL_MS })
  const members = team.data?.members ?? []
  const chosen = members.find((member) => member.operator_id === chosenId) ?? null
  const choose = (id: string) => {
    const next = new URLSearchParams(params)
    if (id) next.set("with", id)
    else next.delete("with")
    setParams(next, { replace: true })
  }
  return (
    <Page title={TEAM.title}>
      <p className="m-0 max-w-[720px] text-[13px] text-muted-foreground">{TEAM.lead}</p>
      {team.error ? (
        <p className="m-0 rounded-[var(--radius-m)] border border-line bg-card px-4 py-3 text-[13px]">
          {errorText(team.error)}
        </p>
      ) : null}
      <div className="grid min-h-[420px] grid-cols-[260px_minmax(0,1fr)] gap-5 max-[760px]:grid-cols-1">
        <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Teammates">
          {team.data && !members.length ? (
            <li className="px-2 py-3 text-[13px] text-muted-foreground">{TEAM.nobody}</li>
          ) : null}
          {members.map((member) => (
            <li key={member.operator_id}>
              <MemberRow
                member={member}
                chosen={member.operator_id === chosenId}
                onPick={() => choose(member.operator_id)}
              />
            </li>
          ))}
        </ul>
        {chosen ? (
          <Thread key={chosen.operator_id} member={chosen} />
        ) : (
          <div className="grid place-items-center rounded-[var(--radius-l)] border border-dashed border-line text-[13px] text-muted-foreground">
            {TEAM.pick}
          </div>
        )}
      </div>
    </Page>
  )
}

function awake(member: Teammate, now = Date.now()): boolean {
  return member.last_seen_at !== "" && now - new Date(member.last_seen_at).getTime() < AWAKE_MS
}

function MemberRow({ member, chosen, onPick }: { member: Teammate; chosen: boolean; onPick: () => void }) {
  const up = awake(member)
  return (
    <button
      type="button"
      aria-pressed={chosen}
      onClick={onPick}
      className={cn(
        "flex w-full cursor-pointer items-center gap-3 rounded-[var(--radius-s)] border-0 bg-transparent px-2 py-2 text-left transition-colors duration-fast ease-out hover:bg-secondary",
        chosen && "bg-surface-2",
      )}
    >
      <span className="relative grid size-8 shrink-0 place-items-center rounded-full bg-secondary text-[11px] font-semibold">
        {initials(member.name || member.email)}
        <span
          aria-label={up ? TEAM.awake : TEAM.away}
          className={cn(
            "absolute -right-0.5 -bottom-0.5 size-2.5 rounded-full border-2 border-card",
            up ? "bg-ok" : "bg-faint",
          )}
        />
      </span>
      <span className="min-w-0 flex-1 leading-[1.25]">
        <span className="block truncate text-[13px] font-semibold">{member.name || member.email}</span>
        <span className="block truncate text-[11px] text-muted-foreground">
          {member.last_seen_at ? SETS.seen(member.last_seen_at) : SETS.neverSeen}
        </span>
      </span>
    </button>
  )
}

function Thread({ member }: { member: Teammate }) {
  const client = useQueryClient()
  const key = THREAD_KEY(member.operator_id)
  const thread = useQuery({
    queryKey: key,
    queryFn: () => fetchThread(member.operator_id),
    refetchInterval: THREAD_POLL_MS,
  })
  const [text, setText] = useState("")
  const send = useMutation({
    mutationFn: (asked: string) => askAgent(member.operator_id, asked),
    onSuccess: (messages) => {
      client.setQueryData(key, messages)
      setText("")
    },
  })
  const end = useRef<HTMLDivElement>(null)
  const count = thread.data?.length ?? 0
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" })
  }, [count])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const asked = text.trim()
    if (asked && !send.isPending) send.mutate(asked)
  }
  const name = member.name || member.email
  return (
    <section
      className="flex min-h-0 flex-col rounded-[var(--radius-l)] border border-line bg-card"
      aria-label={`Thread with ${TEAM.agent(name)}`}
    >
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <span className="text-[13px] font-semibold">{TEAM.agent(name)}</span>
        <span className="text-[11px] text-muted-foreground">
          {awake(member) ? TEAM.awake : member.last_seen_at ? SETS.seen(member.last_seen_at) : SETS.neverSeen}
        </span>
      </header>
      <ol className="m-0 flex max-h-[520px] min-h-[240px] list-none flex-col gap-3 overflow-y-auto p-4">
        {thread.data && !thread.data.length ? (
          <li className="text-[13px] text-muted-foreground">{TEAM.empty}</li>
        ) : null}
        {(thread.data ?? []).map((message) => (
          <Line
            key={message.id}
            message={message}
            who={message.direction === "out" ? TEAM.you : TEAM.agent(name)}
          />
        ))}
        <div ref={end} />
      </ol>
      <form onSubmit={submit} className="flex items-end gap-2 border-t border-line p-3">
        <textarea
          aria-label={TEAM.placeholder}
          placeholder={TEAM.placeholder}
          value={text}
          rows={2}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) submit(event)
          }}
          className="min-h-[44px] flex-1 resize-none rounded-[var(--radius-s)] border border-line bg-transparent px-3 py-2 text-[13px] text-foreground outline-none focus:border-primary"
        />
        <Button variant="primary" type="submit" disabled={send.isPending || !text.trim()}>
          {send.isPending ? TEAM.sending : TEAM.send}
        </Button>
      </form>
      {send.error ? <p className="m-0 px-4 pb-3 text-[12px] text-bad">{errorText(send.error)}</p> : null}
    </section>
  )
}

function Line({ message, who }: { message: SupportMessage; who: string }) {
  const out = message.direction === "out"
  return (
    <li className={cn("flex flex-col gap-1", out ? "items-end" : "items-start")}>
      <span className="text-[11px] text-faint">
        {who} · {SETS.seen(message.at).replace("Seen ", "")}
      </span>
      <div
        className={cn(
          "max-w-[85%] rounded-[var(--radius-m)] px-3 py-2 text-[13px]",
          out ? "bg-primary-soft" : "border border-line bg-background",
        )}
      >
        {out ? <span className="whitespace-pre-wrap">{message.text}</span> : <Markdown text={message.text} />}
      </div>
    </li>
  )
}
