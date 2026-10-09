import { useEffect, useRef, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"

import { EmptyState, initials } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { signIn } from "@/lib/api/feedback"
import type { ReceivedInvite, SetMember, SetView } from "@/lib/api/sets"
import { SETS } from "@/lib/people/copy"
import { useSets } from "@/pages/people/sets/useSets"

// The local sets page, laid out like the Powerset app's Sets page: a set picker and New set in the
// head, invites waiting for an answer, then the chosen set's card with its people, invite box and
// members (role and relay presence). Sets live on this machine; invites ride the relay.
export function SetsPage() {
  const sets = useSets()
  const [params, setParams] = useSearchParams()
  const local = (sets.data?.sets ?? []).filter((set) => !set.is_personal)
  const chosen = local.find((set) => set.set_id === params.get("set")) ?? local[0]
  const choose = (set_id: string) => setParams(set_id ? { set: set_id } : {}, { replace: true })
  const [naming, setNaming] = useState(false)
  const [confirming, setConfirming] = useState(false)

  return (
    <main className="overflow-y-auto px-5 py-6 max-[680px]:px-4">
      <div className="mx-auto flex max-w-[960px] flex-col gap-5">
        {/* Two columns at every width but a phone's: the text takes what is left, the controls their own width. */}
        <header className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-4 max-[680px]:grid-cols-1">
          <div className="min-w-0">
            <h1 className="m-0 text-xl font-semibold">{SETS.title}</h1>
            <p className="m-0 mt-1 text-[13px] text-muted-foreground">{SETS.lead}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {local.length ? (
              <span className="relative inline-flex">
                <select
                  aria-label={SETS.choose}
                  value={chosen?.set_id ?? ""}
                  onChange={(event) => {
                    choose(event.target.value)
                    setConfirming(false)
                  }}
                  className="h-8 min-w-[220px] cursor-pointer appearance-none rounded-[var(--radius-s)] border border-line-strong bg-background pl-2.5 pr-8 text-[13px] text-foreground"
                >
                  {local.map((set) => (
                    <option key={set.set_id} value={set.set_id}>
                      {set.name}
                    </option>
                  ))}
                </select>
                <svg
                  viewBox="0 0 16 16"
                  aria-hidden="true"
                  className="pointer-events-none absolute right-2.5 top-1/2 size-3 -translate-y-1/2 text-muted-foreground"
                >
                  <path
                    d="M4 6l4 4 4-4"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
            ) : null}
            <Button size="sm" variant="primary" onClick={() => setNaming(true)}>
              + {SETS.newSet}
            </Button>
          </div>
        </header>

        {naming ? (
          <NewSet
            busy={sets.busy}
            onCancel={() => setNaming(false)}
            onCreate={(name) =>
              sets.create(name).then((payload) => {
                setNaming(false)
                const made = payload.sets.find((set) => !set.is_personal && set.name === name)
                if (made) choose(made.set_id)
              })
            }
          />
        ) : null}

        {sets.failure ? (
          <div className="flex items-center gap-3 rounded-[var(--radius-m)] border border-line bg-card px-4 py-3 text-[13px]">
            <span>{sets.failure.needsAuth ? SETS.signInNeeded : sets.failure.message}</span>
            {sets.failure.needsAuth ? (
              <Button size="sm" onClick={() => void signIn().then(sets.reload).catch(sets.fail)}>
                {SETS.signIn}
              </Button>
            ) : null}
          </div>
        ) : null}

        {sets.data?.invites.length ? (
          <section className="flex flex-col gap-2" aria-label={SETS.invites}>
            <h2 className="m-0 text-[10.5px] font-bold uppercase tracking-[.08em] text-faint">
              {SETS.invites}
            </h2>
            {sets.data.invites.map((invite) => (
              <InviteCard
                key={invite.id}
                invite={invite}
                busy={sets.busy}
                onAnswer={(accepted) => sets.answer(invite.id, accepted)}
              />
            ))}
          </section>
        ) : null}

        {sets.isPending ? <EmptyState>{SETS.loading}</EmptyState> : null}
        {sets.data && !local.length && !naming ? (
          <div className="rounded-[var(--radius-l)] border border-dashed border-line px-6 py-14 text-center">
            <p className="m-0 text-[15px] font-semibold">{SETS.noneTitle}</p>
            <p className="m-0 mt-1 text-[13px] text-muted-foreground">{SETS.none}</p>
          </div>
        ) : null}

        {chosen ? (
          <SetCard
            key={chosen.set_id}
            set={chosen}
            busy={sets.busy}
            confirming={confirming}
            onConfirm={setConfirming}
            onInvite={(email) => sets.invite(chosen.set_id, email)}
            onDelete={() =>
              void sets.remove(chosen.set_id).then(() => {
                setConfirming(false)
                choose("")
              })
            }
          />
        ) : null}
      </div>
    </main>
  )
}

function NewSet({
  busy,
  onCreate,
  onCancel,
}: {
  busy: boolean
  onCreate: (name: string) => Promise<unknown>
  onCancel: () => void
}) {
  const [name, setName] = useState("")
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (name.trim()) void onCreate(name.trim()).catch(() => undefined)
  }
  return (
    <form
      onSubmit={submit}
      className="flex items-center gap-2 rounded-[var(--radius-m)] border border-line bg-card p-3"
    >
      <input
        ref={field}
        value={name}
        placeholder={SETS.namePlaceholder}
        aria-label={SETS.namePlaceholder}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel()
        }}
        className="h-8 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-background px-2.5 text-[13px] text-foreground"
      />
      <Button size="sm" variant="ghost" type="button" onClick={onCancel}>
        {SETS.cancel}
      </Button>
      <Button size="sm" variant="primary" type="submit" disabled={busy || !name.trim()}>
        {SETS.create}
      </Button>
    </form>
  )
}

function InviteCard({
  invite,
  busy,
  onAnswer,
}: {
  invite: ReceivedInvite
  busy: boolean
  onAnswer: (accepted: boolean) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-[var(--radius-m)] border border-[color-mix(in_srgb,var(--primary)_35%,transparent)] bg-card px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="m-0 text-[14px] font-semibold">{invite.set_name}</p>
        <p className="m-0 mt-0.5 break-words text-[12.5px] text-muted-foreground">
          {SETS.invitedYou(invite.from, invite.from_email)}
        </p>
      </div>
      <div className="flex shrink-0 gap-2">
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAnswer(false)}>
          {SETS.decline}
        </Button>
        <Button size="sm" variant="primary" disabled={busy} onClick={() => onAnswer(true)}>
          {SETS.accept}
        </Button>
      </div>
    </div>
  )
}

interface SetCardProps {
  set: SetView
  busy: boolean
  confirming: boolean
  onConfirm: (confirming: boolean) => void
  onInvite: (email: string) => Promise<unknown>
  onDelete: () => void
}

function SetCard({ set, busy, confirming, onConfirm, onInvite, onDelete }: SetCardProps) {
  const owner = set.role === "owner"
  return (
    <section className="rounded-[var(--radius-l)] border border-line bg-card shadow-[var(--shadow-1)]">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <h2 className="m-0 break-words text-lg font-semibold">{set.name}</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Badge variant="info">{SETS.people(set.person_count)}</Badge>
            <Badge variant="muted">{SETS.members(set.member_count)}</Badge>
            <span className="text-xs text-muted-foreground">{owner ? SETS.youOwn : SETS.youJoined}</span>
          </div>
        </div>
        {confirming ? (
          <div className="flex items-center gap-2 text-[12.5px]">
            <span>{owner ? SETS.confirmDelete(set.name) : SETS.confirmLeave(set.name)}</span>
            <Button size="sm" variant="bad" disabled={busy} onClick={onDelete}>
              {owner ? SETS.delete : SETS.leave}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onConfirm(false)}>
              {SETS.keep}
            </Button>
          </div>
        ) : (
          <Button size="sm" variant="ghost" onClick={() => onConfirm(true)}>
            {owner ? SETS.delete : SETS.leave}
          </Button>
        )}
      </div>
      {owner ? (
        <div className="border-b border-line px-5 py-3">
          <InviteForm onInvite={onInvite} busy={busy} />
        </div>
      ) : null}
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="text-left text-[10.5px] uppercase tracking-[.08em] text-faint">
            <th className="px-5 py-2.5 font-bold">{SETS.memberHead}</th>
            <th className="px-3 py-2.5 font-bold">{SETS.roleHead}</th>
            <th className="px-3 py-2.5 text-right font-bold">{SETS.sharesHead}</th>
            <th className="px-5 py-2.5 text-right font-bold">{SETS.statusHead}</th>
          </tr>
        </thead>
        <tbody>
          {set.members.map((member) => (
            <tr key={member.operator_id || member.email} className="border-t border-line">
              <td className="px-5 py-3">
                <Identity name={member.name} email={member.email} />
              </td>
              <td className="px-3 py-3 capitalize text-muted-foreground">{member.role}</td>
              <td className="px-3 py-3 text-right tabular-nums text-muted-foreground">
                {SETS.people(member.person_count)}
              </td>
              <td className="px-5 py-3 text-right">
                <Presence member={member} />
              </td>
            </tr>
          ))}
          {set.invited.map((invite) => (
            <tr key={invite.id} className="border-t border-line text-muted-foreground">
              <td className="px-5 py-3">
                <Identity name="" email={invite.email} />
              </td>
              <td className="px-3 py-3">—</td>
              <td className="px-3 py-3 text-right">—</td>
              <td className="px-5 py-3 text-right">
                <Badge variant={invite.status === "declined" ? "bad" : "warn"}>
                  {invite.status === "declined" ? SETS.declined : SETS.invited}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function Identity({ name, email }: { name: string; email: string }) {
  const shown = name || email
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <span
        aria-hidden="true"
        className="grid size-7 shrink-0 place-items-center rounded-full bg-secondary text-[10px] font-bold text-muted-foreground"
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

function Presence({ member }: { member: SetMember }) {
  if (!member.last_seen_at) return <span className="text-xs text-faint">{SETS.neverSeen}</span>
  const label = SETS.seen(member.last_seen_at)
  const live = label === SETS.connected
  return (
    <span
      data-live={live}
      className="inline-flex items-center gap-1.5 text-xs text-faint data-[live=true]:text-ok"
    >
      <span
        aria-hidden="true"
        className={
          live
            ? "size-[7px] rounded-full bg-[var(--ok)] shadow-[0_0_0_3px_var(--ok-soft)]"
            : "size-[7px] rounded-full bg-[var(--faint)]"
        }
      />
      {label}
    </span>
  )
}

function InviteForm({ onInvite, busy }: { onInvite: (email: string) => Promise<unknown>; busy: boolean }) {
  const [email, setEmail] = useState("")
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!email.includes("@")) return
    void onInvite(email.trim()).then(
      () => setEmail(""),
      () => undefined,
    )
  }
  return (
    <form onSubmit={submit} className="flex max-w-[420px] items-center gap-2">
      <input
        type="email"
        value={email}
        placeholder={SETS.invitePlaceholder}
        aria-label={SETS.invitePlaceholder}
        onChange={(event) => setEmail(event.target.value)}
        className="h-8 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-background px-2.5 text-[13px] text-foreground"
      />
      <Button size="sm" variant="primary" type="submit" disabled={busy || !email.includes("@")}>
        {SETS.invite}
      </Button>
    </form>
  )
}
