import { useMutation, useQuery } from "@tanstack/react-query"
import { useSearchParams } from "react-router-dom"

import { LinkIcon, Page, Select } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { offeredSets, setName, useSearchScope } from "@/lib/agent/useSearchScope"
import { fetchSetDetail, inviteToSet } from "@/lib/api/searches"
import { errorText } from "@/lib/api/http"
import { SETS } from "@/lib/people/copy"

import { CopyButton } from "../agent/CopyButton"

const LOCAL = "local"

// Laid out as the Powerset app's Sets page: a set picker in the head, then the chosen set's card with
// its description, contacts and members, and for a set you own an Invite button that makes a link.
// The picker offers what Search in offers: this Mac's local index, then your Powerset sets without
// the cloud copy of your own network.
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
  const invite = useMutation({ mutationFn: () => inviteToSet(setId) })
  if (detail.error) return <p className="m-0 text-[13px] text-bad">{errorText(detail.error)}</p>
  if (!detail.data) return <SetCardSkeleton />
  const set = detail.data
  return (
    <section className={`${CARD} px-5 py-5`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="m-0 break-words text-lg font-semibold">{set.name}</h2>
          <p className="m-0 mt-1 text-[13px] italic text-muted-foreground">
            {set.description ?? SETS.noDescription}
          </p>
          <Badge variant="outline" className="mt-3">
            {SETS.uniqueContacts(set.person_count)}
          </Badge>
          <p className="m-0 mt-3 text-xs text-muted-foreground">{SETS.members(set.member_count)}</p>
        </div>
        {set.role === "owner" ? (
          <Button variant="outline" disabled={invite.isPending} onClick={() => invite.mutate()}>
            <LinkIcon />
            {SETS.invite}
          </Button>
        ) : null}
      </div>
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
