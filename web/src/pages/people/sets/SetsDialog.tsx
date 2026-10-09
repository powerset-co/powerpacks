import { useEffect, useRef, useState, type FormEvent } from "react"
import { Fold, initials } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { signIn } from "@/lib/api/feedback"
import type { SetView } from "@/lib/api/sets"
import { SETS } from "@/lib/people/copy"
import { cn } from "@/lib/utils"
import { CloudIcon, HomeIcon } from "./icons"
import type { useSets } from "./useSets"

interface SetsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  sets: ReturnType<typeof useSets>
}

function Members({ set }: { set: SetView }) {
  return (
    <ul className="set-members">
      {set.members.map((member) => (
        <li key={member.email || member.name}>
          <span className="set-avatar" aria-hidden="true">
            {initials(member.name || member.email)}
          </span>
          <span className="set-member">
            <span className="set-member-name">{member.name || member.email}</span>
            {member.name && member.email ? <span className="set-member-email">{member.email}</span> : null}
          </span>
          {member.role !== "member" ? <span className="set-role">{member.role}</span> : null}
        </li>
      ))}
    </ul>
  )
}

interface RowProps {
  set: SetView
  shared: number
  open: boolean
  onToggle: () => void
  onDelete: (() => void) | null
}

function SetRow({ set, shared, open, onToggle, onDelete }: RowProps) {
  return (
    <li className="set-row" data-open={open} data-personal={set.is_personal}>
      <button type="button" className="set-head" aria-expanded={open} onClick={onToggle}>
        {set.is_personal ? <HomeIcon className="set-icon local" /> : <CloudIcon className="set-icon" />}
        <span className="set-name">{set.name}</span>
        <span className="set-meta">
          {set.is_personal ? <span className="set-tag">{SETS.local}</span> : null}
          <span className="set-count">
            {set.is_personal ? SETS.shared(shared) : SETS.members(set.member_count)}
          </span>
        </span>
      </button>
      <Fold open={open}>
        <div className="set-body">
          <Members set={set} />
          <div className="set-foot">
            <span>{set.is_personal ? SETS.personalNote : SETS.people(set.person_count)}</span>
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

// The sets dialog: the personal (local) network, then the cloud sets the owner belongs to, each unfolding
// to its members; New set creates one in the cloud; the owner of a set can delete it.
export function SetsDialog({ open, onOpenChange, sets }: SetsDialogProps) {
  const [unfolded, setUnfolded] = useState("")
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState("")
  const [confirming, setConfirming] = useState("")
  const nameField = useRef<HTMLInputElement>(null)
  // The name box takes focus once it is on screen.
  useEffect(() => {
    if (naming) nameField.current?.focus()
  }, [naming])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!name.trim()) return
    void sets.create(name.trim()).then(
      () => {
        setNaming(false)
        setName("")
      },
      () => undefined,
    )
  }
  const shown = sets.data?.sets ?? []
  const shared = sets.data?.shared ?? 0
  const personal = shown.filter((set) => set.is_personal)
  const cloud = shown.filter((set) => !set.is_personal)
  const row = (set: SetView) => (
    <SetRow
      key={set.set_id}
      set={set}
      shared={shared}
      open={unfolded === set.set_id}
      onToggle={() => setUnfolded(unfolded === set.set_id ? "" : set.set_id)}
      onDelete={set.role === "owner" && !set.is_personal ? () => setConfirming(set.set_id) : null}
    />
  )
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-4 p-6 sets-dialog" onKeyDown={(event) => event.stopPropagation()}>
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">{SETS.title}</DialogTitle>
          <DialogDescription className="text-[13px]">{SETS.lead}</DialogDescription>
        </DialogHeader>
        <div className="sets-toolbar">
          <button type="button" className="sets-refresh" disabled={sets.busy} onClick={sets.refresh}>
            <svg viewBox="0 0 16 16" aria-hidden="true" className={cn(sets.refreshing && "turning")}>
              <path
                d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            {SETS.refresh}
          </button>
        </div>
        {sets.failure ? (
          <div className="sets-error">
            <span>{sets.failure.needsAuth ? SETS.signInNeeded : sets.failure.message}</span>
            {sets.failure.needsAuth ? (
              <Button size="sm" onClick={() => void signIn().then(sets.refresh).catch(sets.fail)}>
                {SETS.signIn}
              </Button>
            ) : null}
          </div>
        ) : null}
        <ul className="set-list">{personal.map(row)}</ul>
        {sets.data && !sets.failure ? (
          <p className="sets-divider">{cloud.length ? SETS.sharedWith : SETS.none}</p>
        ) : null}
        <ul className="set-list">{cloud.map(row)}</ul>
        {confirming ? (
          <div className="sets-confirm">
            <span>{SETS.confirmDelete(shown.find((set) => set.set_id === confirming)?.name ?? "")}</span>
            <Button
              size="sm"
              variant="bad"
              disabled={sets.busy}
              onClick={() =>
                void sets.remove(confirming).then(
                  () => setConfirming(""),
                  () => undefined,
                )
              }
            >
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
            <Button size="sm" variant="primary" type="submit" disabled={sets.busy || !name.trim()}>
              {SETS.create}
            </Button>
          </form>
        ) : (
          <button type="button" className="sets-add" onClick={() => setNaming(true)}>
            + {SETS.newSet}
          </button>
        )}
      </DialogContent>
    </Dialog>
  )
}
