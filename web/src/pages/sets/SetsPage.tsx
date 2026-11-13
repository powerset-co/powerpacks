import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"

import { LinkIcon, Page, PlusIcon, TrashIcon } from "@/components/shared"
import { ScopePicker } from "@/components/shared/ScopePicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { offeredSets, SEARCH_SCOPE_KEY, useSearchScope } from "@/lib/agent/useSearchScope"
import { createSet, deleteSet, fetchSetDetail, inviteToSet } from "@/lib/api/searches"
import { errorText } from "@/lib/api/http"
import { SETS } from "@/lib/people/copy"

import { CopyButton } from "../agent/CopyButton"

const LOCAL = "local"

// Laid out as the Powerset app's Sets page: a set picker in the head, then the chosen set's card with
// its contacts and members; New Set makes one, and on a set you own Invite makes a link and the bin
// deletes it.
// The picker is the chat's Search-in picker, so a set reads the same here as there: this Mac's local
// index, and your Powerset sets with their counts, without the cloud copy of your own network.
export function SetsPage() {
  const scope = useSearchScope()
  const [params, setParams] = useSearchParams()
  const local = scope.data?.local_db ?? null
  const sets = scope.data ? offeredSets(scope.data) : []
  const offered = [...(local ? [LOCAL] : []), ...sets.map((set) => set.set_id)]
  const chosen = offered.find((value) => value === params.get("set")) ?? offered[0]
  const choose = (value: string) => setParams(value ? { set: value } : {}, { replace: true })
  const [naming, setNaming] = useState(false)

  return (
    <Page title={SETS.title}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{SETS.lead}</p>
        <div className="flex items-center gap-2">
          <ScopePicker
            value={chosen === LOCAL && local ? { local_db: local } : chosen ? { set_id: chosen } : null}
            onChange={(next) => choose("set_id" in next ? next.set_id : LOCAL)}
            placeholder={SETS.noneTitle}
            title={SETS.choose}
            className="min-w-[220px]"
          />
          <Button variant="primary" className="h-8" onClick={() => setNaming(true)}>
            <PlusIcon />
            {SETS.newSet}
          </Button>
        </div>
      </div>
      {naming ? (
        <NewSet
          onCancel={() => setNaming(false)}
          onCreated={(id) => {
            setNaming(false)
            choose(id)
          }}
        />
      ) : null}
      {scope.error ? <p className="m-0 text-[13px] text-bad">{errorText(scope.error)}</p> : null}
      {scope.isPending ? <SetCardSkeleton /> : null}
      {scope.data && !chosen ? (
        <div className="rounded-[var(--radius-l)] border border-dashed border-line px-6 py-14 text-center">
          <p className="m-0 text-[15px] font-semibold">{SETS.noneTitle}</p>
        </div>
      ) : null}
      {chosen === LOCAL ? (
        <LocalCard />
      ) : chosen ? (
        <SetCard
          key={chosen}
          setId={chosen}
          personal={sets.some((set) => set.set_id === chosen && set.is_personal)}
          onDeleted={() => choose("")}
        />
      ) : null}
    </Page>
  )
}

const CARD = "rounded-[var(--radius-l)] border border-line bg-card shadow-[var(--shadow-1)]"

function LocalCard() {
  return (
    <section className={CARD}>
      <div className="px-5 py-4">
        <h2 className="m-0 text-base font-semibold">{SETS.localIndex}</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge variant="info">{SETS.thisMac}</Badge>
          <span className="text-xs text-muted-foreground">{SETS.localNote}</span>
        </div>
      </div>
    </section>
  )
}

function NewSet({ onCancel, onCreated }: { onCancel: () => void; onCreated: (id: string) => void }) {
  const client = useQueryClient()
  const [text, setText] = useState("")
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])
  const create = useMutation({
    mutationFn: (name: string) => createSet(name),
    onSuccess: async ({ id }) => {
      await client.invalidateQueries({ queryKey: SEARCH_SCOPE_KEY })
      onCreated(id)
    },
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (text.trim()) create.mutate(text.trim())
  }
  return (
    <form
      onSubmit={submit}
      className="flex flex-wrap items-center gap-2 rounded-[var(--radius-m)] border border-line bg-card p-3"
    >
      <input
        ref={field}
        value={text}
        placeholder={SETS.namePlaceholder}
        aria-label={SETS.namePlaceholder}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel()
        }}
        className="h-8 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-background px-2.5 text-[13px] text-foreground"
      />
      <Button variant="ghost" type="button" className="h-8" onClick={onCancel}>
        {SETS.cancel}
      </Button>
      <Button variant="primary" type="submit" className="h-8" disabled={create.isPending || !text.trim()}>
        {SETS.create}
      </Button>
      {create.error ? <p className="m-0 w-full text-[13px] text-bad">{errorText(create.error)}</p> : null}
    </form>
  )
}

// Your personal set (your own network in the cloud) has no Delete.
function SetCard({
  setId,
  personal,
  onDeleted,
}: {
  setId: string
  personal: boolean
  onDeleted: () => void
}) {
  const client = useQueryClient()
  const detail = useQuery({ queryKey: ["sets", setId], queryFn: () => fetchSetDetail(setId) })
  const invite = useMutation({ mutationFn: () => inviteToSet(setId) })
  const [confirming, setConfirming] = useState(false)
  const remove = useMutation({
    mutationFn: () => deleteSet(setId),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: SEARCH_SCOPE_KEY })
      onDeleted()
    },
  })
  if (detail.error) return <p className="m-0 text-[13px] text-bad">{errorText(detail.error)}</p>
  if (!detail.data) return <SetCardSkeleton />
  const set = detail.data
  return (
    <section className={`${CARD} px-5 py-5`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="m-0 break-words text-lg font-semibold">{set.name}</h2>
          <Badge variant="outline" className="mt-3">
            {SETS.uniqueContacts(set.person_count)}
          </Badge>
          <p className="m-0 mt-3 text-xs text-muted-foreground">{SETS.members(set.member_count)}</p>
        </div>
        {set.role !== "owner" ? null : confirming ? (
          <div className="flex items-center gap-2 text-[13px]">
            <span>{SETS.confirmDelete(set.name)}</span>
            <Button size="sm" variant="bad" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {SETS.delete}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
              {SETS.keep}
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-1">
            <Button variant="outline" disabled={invite.isPending} onClick={() => invite.mutate()}>
              <LinkIcon />
              {SETS.invite}
            </Button>
            {personal ? null : (
              <Button
                variant="ghost"
                size="icon"
                aria-label={SETS.delete}
                onClick={() => setConfirming(true)}
              >
                <TrashIcon />
              </Button>
            )}
          </div>
        )}
      </div>
      {remove.error ? <p className="m-0 mt-3 text-[13px] text-bad">{errorText(remove.error)}</p> : null}
      {invite.error ? <p className="m-0 mt-3 text-[13px] text-bad">{errorText(invite.error)}</p> : null}
      {invite.data ? (
        <div className="mt-3 flex items-center gap-2 text-[13px]">
          <span className="text-muted-foreground">{SETS.inviteLink}</span>
          <span className="min-w-0 truncate font-medium">{invite.data.invite_url}</span>
          <CopyButton text={invite.data.invite_url} />
        </div>
      ) : null}
      <table className="mt-4 w-full border-collapse text-[13px]">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="px-3 py-2.5 font-medium">{SETS.userHead}</th>
            <th className="px-3 py-2.5 font-medium">{SETS.roleHead}</th>
            <th className="px-3 py-2.5 text-right font-medium">{SETS.contactsHead}</th>
            <th className="px-3 py-2.5 font-medium">{SETS.joinedHead}</th>
          </tr>
        </thead>
        <tbody>
          {set.members.map((member) => (
            <tr key={member.user_id} className="border-t border-line">
              <td className="px-3 py-3">
                <span className="font-semibold">{member.name ?? member.email}</span>
                {member.name && member.email ? (
                  <span className="ml-2 text-xs text-faint">{member.email}</span>
                ) : null}
              </td>
              <td className="px-3 py-3">
                <Badge
                  variant="muted"
                  className={member.role === "owner" ? "bg-primary-action text-white" : undefined}
                >
                  {member.role}
                </Badge>
              </td>
              <td className="px-3 py-3 text-right tabular-nums">{member.person_count.toLocaleString()}</td>
              <td className="px-3 py-3 text-muted-foreground">
                {new Date(member.joined_at).toLocaleDateString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

/** The set card's shape while it loads, so nothing shifts when it lands. */
function SetCardSkeleton() {
  return (
    <section aria-label={SETS.loading} className={CARD}>
      <div className="border-b border-line px-5 py-4">
        <Skeleton className="h-7 w-48 bg-surface-2" />
        <div className="mt-2 flex gap-2">
          <Skeleton className="h-5 w-20 rounded-full bg-surface-2" />
          <Skeleton className="h-5 w-24 rounded-full bg-surface-2" />
        </div>
      </div>
      {[0, 1].map((row) => (
        <div key={row} className="flex items-center gap-2.5 border-t border-line px-5 py-3">
          <Skeleton className="size-7 shrink-0 rounded-full bg-surface-2" />
          <Skeleton className="h-4 w-56 bg-surface-2" />
        </div>
      ))}
    </section>
  )
}
