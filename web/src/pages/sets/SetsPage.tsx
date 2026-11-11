import { useMutation, useQuery } from "@tanstack/react-query"
import { useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"

import { initials, Page, Select } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { offeredSets, setName, useSearchScope } from "@/lib/agent/useSearchScope"
import { fetchSetDetail, inviteToSet } from "@/lib/api/searches"
import { errorText } from "@/lib/api/http"
import { SETS } from "@/lib/people/copy"

const LOCAL = "local"

// Laid out as the old Sets page was: a set picker in the head, then the chosen set's card with its
// people, members and your role, and for a set you own an invite box. The picker offers what Search in offers: this Mac's local index,
// then your Powerset sets without the cloud copy of your own network.
export function SetsPage() {
  const scope = useSearchScope()
  const [params, setParams] = useSearchParams()
  const sets = scope.data ? offeredSets(scope.data) : []
  const options = [
    ...(scope.data?.local_db ? [{ value: LOCAL, label: SETS.localIndex }] : []),
    ...sets.map((set) => ({ value: set.set_id, label: setName(set) })),
  ]
  const chosen = options.find(({ value }) => value === params.get("set"))?.value ?? options[0]?.value

  return (
    <Page
      title={SETS.title}
      actions={
        <Select
          aria-label={SETS.choose}
          value={chosen ?? ""}
          onChange={(value) => setParams(value ? { set: value } : {}, { replace: true })}
          disabled={!options.length}
          className="min-w-[220px]"
          placeholder={scope.isPending ? SETS.loading : SETS.noneTitle}
          options={options}
        />
      }
    >
      <p className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{SETS.lead}</p>
      {scope.error ? <p className="m-0 text-[13px] text-bad">{errorText(scope.error)}</p> : null}
      {scope.isPending ? <SetCardSkeleton /> : null}
      {scope.data && !chosen ? (
        <div className="rounded-[var(--radius-l)] border border-dashed border-line px-6 py-14 text-center">
          <p className="m-0 text-[15px] font-semibold">{SETS.noneTitle}</p>
        </div>
      ) : null}
      {chosen === LOCAL ? <LocalCard /> : chosen ? <SetCard key={chosen} setId={chosen} /> : null}
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

function SetCard({ setId }: { setId: string }) {
  const detail = useQuery({ queryKey: ["sets", setId], queryFn: () => fetchSetDetail(setId) })
  if (detail.error) return <p className="m-0 text-[13px] text-bad">{errorText(detail.error)}</p>
  if (!detail.data) return <SetCardSkeleton />
  const set = detail.data
  return (
    <section className={CARD}>
      <div className="border-b border-line px-5 py-4">
        <h2 className="m-0 break-words text-base font-semibold">{set.name}</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge variant="info">{SETS.people(set.person_count)}</Badge>
          <Badge variant="muted">{SETS.members(set.member_count)}</Badge>
          <span className="text-xs text-muted-foreground">
            {set.role === "owner" ? SETS.youOwn : SETS.youJoined}
          </span>
        </div>
      </div>
      {set.role === "owner" ? (
        <div className="border-b border-line px-5 py-3">
          <InviteForm setId={set.id} />
        </div>
      ) : null}
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-[.08em] text-faint">
            <th className="px-5 py-2.5 font-bold">{SETS.memberHead}</th>
            <th className="px-3 py-2.5 font-bold">{SETS.roleHead}</th>
            <th className="px-5 py-2.5 text-right font-bold">{SETS.sharesHead}</th>
          </tr>
        </thead>
        <tbody>
          {set.members.map((member) => (
            <tr key={member.user_id} className="border-t border-line">
              <td className="px-5 py-3">
                <Identity name={member.name ?? ""} email={member.email ?? ""} />
              </td>
              <td className="px-3 py-3 capitalize text-muted-foreground">{member.role}</td>
              <td className="px-5 py-3 text-right tabular-nums text-muted-foreground">
                {SETS.people(member.person_count)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function InviteForm({ setId }: { setId: string }) {
  const [email, setEmail] = useState("")
  const invite = useMutation({ mutationFn: (to: string) => inviteToSet(setId, to) })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (email.includes("@")) invite.mutate(email.trim(), { onSuccess: () => setEmail("") })
  }
  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={submit} className="flex max-w-[420px] items-center gap-2">
        <input
          type="email"
          value={email}
          placeholder={SETS.invitePlaceholder}
          aria-label={SETS.invitePlaceholder}
          onChange={(event) => setEmail(event.target.value)}
          className="h-8 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-background px-2.5 text-[13px] text-foreground"
        />
        <Button
          variant="primary"
          type="submit"
          className="h-8"
          disabled={invite.isPending || !email.includes("@")}
        >
          {SETS.invite}
        </Button>
      </form>
      {invite.error ? <p className="m-0 text-[13px] text-bad">{errorText(invite.error)}</p> : null}
      {invite.data ? (
        <p className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
          {SETS.invited(invite.variables)} {invite.data.invite_url}
        </p>
      ) : null}
    </div>
  )
}

function Identity({ name, email }: { name: string; email: string }) {
  const shown = name || email
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <span
        aria-hidden="true"
        className="grid size-7 shrink-0 place-items-center rounded-full bg-secondary text-[11px] font-bold text-muted-foreground"
      >
        {initials(shown)}
      </span>
      <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{shown}</span>
        {name && email && name !== email ? <span className="text-xs text-faint">{email}</span> : null}
      </span>
    </span>
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
