import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type FormEvent } from "react"
import { Fold, initials } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { signIn } from "@/lib/api/feedback"
import { createSet, deleteSet, fetchSets, SetsError, type SetsPayload, type SetView } from "@/lib/api/sets"
import { SETS } from "@/lib/people/copy"
import { cn } from "@/lib/utils"
import "../styles/sets.css"

const KEY = ["people", "sets"] as const

function CloudIcon() {
  return (
    <svg className="set-cloud" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M4.5 12.5h7a3 3 0 0 0 .4-5.97A4 4 0 0 0 4.2 7.6 2.5 2.5 0 0 0 4.5 12.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function Members({ set }: { set: SetView }) {
  return (
    <ul className="set-members">
      {set.members.map((member) => (
        <li key={member.email || member.name}>
          <span className="set-avatar" aria-hidden="true">
            {initials(member.name || member.email)}
          </span>
          <span className="set-member-name">{member.name || member.email}</span>
          {member.role !== "member" ? <span className="set-role">{member.role}</span> : null}
        </li>
      ))}
    </ul>
  )
}

interface RowProps {
  set: SetView
  open: boolean
  onToggle: () => void
  onDelete: (() => void) | null
}

function SetRow({ set, open, onToggle, onDelete }: RowProps) {
  return (
    <li className="set-row" data-open={open}>
      <button type="button" className="set-head" aria-expanded={open} onClick={onToggle}>
        <CloudIcon />
        <span className="set-name">{set.name}</span>
        {set.is_personal ? <span className="set-tag">{SETS.personal}</span> : null}
        <span className="set-count">{SETS.members(set.member_count)}</span>
      </button>
      <Fold open={open}>
        <div className="set-body">
          <Members set={set} />
          <div className="set-foot">
            <span>{SETS.people(set.person_count)}</span>
            {onDelete ? (
              <button type="button" className="set-delete" onClick={onDelete}>
                {SETS.delete}
              </button>
            ) : null}
          </div>
        </div>
      </Fold>
    </li>
  )
}

// The sets the owner belongs to, above the filters: who can see the shared network. A set unfolds to
// its members; "New set" creates one in the cloud; the owner of a set can delete it.
export function SetsPanel() {
  const client = useQueryClient()
  const sets = useQuery({ queryKey: KEY, queryFn: () => fetchSets(false) })
  const [open, setOpen] = useState("")
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState("")
  const [confirming, setConfirming] = useState("")
  const [error, setError] = useState<SetsError | null>(null)
  const nameField = useRef<HTMLInputElement>(null)
  // The name box takes focus once it is on screen.
  useEffect(() => {
    if (naming) nameField.current?.focus()
  }, [naming])
  const settle = (payload: SetsPayload) => {
    client.setQueryData(KEY, payload)
    setError(null)
  }
  const fail = (caught: unknown) =>
    setError(caught instanceof SetsError ? caught : new SetsError(errorText(caught), false))
  const refresh = useMutation({ mutationFn: () => fetchSets(true), onSuccess: settle, onError: fail })
  const create = useMutation({
    mutationFn: createSet,
    onSuccess: (payload) => {
      settle(payload)
      setNaming(false)
      setName("")
    },
    onError: fail,
  })
  const remove = useMutation({
    mutationFn: deleteSet,
    onSuccess: (payload) => {
      settle(payload)
      setConfirming("")
    },
    onError: fail,
  })
  const busy = refresh.isPending || create.isPending || remove.isPending
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (name.trim()) create.mutate(name.trim())
  }
  const shown = sets.data?.sets ?? []
  const failure = error ?? (sets.error instanceof SetsError ? sets.error : null)
  return (
    <section className="sets" aria-label={SETS.title}>
      <div className="sets-head">
        <h2>{SETS.title}</h2>
        <button
          type="button"
          className="sets-refresh"
          disabled={busy}
          onClick={() => refresh.mutate()}
          title={SETS.refresh}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true" className={cn(refresh.isPending && "turning")}>
            <path
              d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="sr-only">{SETS.refresh}</span>
        </button>
      </div>
      {sets.data ? (
        <p className="sets-shared">
          <strong>{sets.data.shared.toLocaleString()}</strong> {SETS.sharedLine(shown.length)}
        </p>
      ) : null}
      {failure ? (
        <div className="sets-error">
          <span>{failure.needsAuth ? SETS.signInNeeded : failure.message}</span>
          {failure.needsAuth ? (
            <Button
              size="sm"
              onClick={() =>
                void signIn()
                  .then(() => refresh.mutate())
                  .catch(fail)
              }
            >
              {SETS.signIn}
            </Button>
          ) : null}
        </div>
      ) : null}
      {sets.data && !shown.length && !failure ? <p className="sets-empty">{SETS.none}</p> : null}
      <ul className="set-list">
        {shown.map((set) => (
          <SetRow
            key={set.set_id}
            set={set}
            open={open === set.set_id}
            onToggle={() => setOpen(open === set.set_id ? "" : set.set_id)}
            onDelete={set.role === "owner" && !set.is_personal ? () => setConfirming(set.set_id) : null}
          />
        ))}
      </ul>
      {confirming ? (
        <div className="sets-confirm">
          <span>{SETS.confirmDelete(shown.find((set) => set.set_id === confirming)?.name ?? "")}</span>
          <Button size="sm" variant="bad" disabled={busy} onClick={() => remove.mutate(confirming)}>
            {SETS.delete}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirming("")}>
            {SETS.keep}
          </Button>
        </div>
      ) : null}
      {naming ? (
        <form className="sets-new" onSubmit={submit}>
          <input
            ref={nameField}
            value={name}
            placeholder={SETS.namePlaceholder}
            aria-label={SETS.namePlaceholder}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setNaming(false)
            }}
          />
          <Button size="sm" variant="primary" type="submit" disabled={busy || !name.trim()}>
            {SETS.create}
          </Button>
        </form>
      ) : (
        <button type="button" className="sets-add" onClick={() => setNaming(true)}>
          + {SETS.newSet}
        </button>
      )}
    </section>
  )
}
