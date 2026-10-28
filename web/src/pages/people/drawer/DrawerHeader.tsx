import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Fragment, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react"
import { Check, Pencil, X } from "lucide-react"

import { ProxiedLinkedinAvatar, DrawerClose, LinkedinLink, SourcePill, Tip } from "@/components/shared"
import { searchText, writeName } from "@/lib/api/people"
import { toChannels, type Channel } from "@/lib/channels"
import { monthYear, plural } from "@/lib/copy"
import { label } from "@/lib/people/copy"
import type { Person, PersonDetail } from "@/types/people"

import { PEOPLE_QUERY_KEY } from "../hooks/usePeopleQuery"

interface DrawerHeaderProps {
  row: Person
  detail: PersonDetail | null
  // The icon actions beside Close.
  action: ReactNode
  onClose: () => void
  onRefresh: () => void
}

// The name with a pencil beside it: the pencil opens a field, Enter or the check saves the human's
// name for the family (kept on refresh, like a relationship choice), Escape or the cross drops it.
function Name({ row, onRefresh }: { row: Person; onRefresh: () => void }) {
  const client = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (draft !== null) field.current?.select()
  }, [draft])
  const save = useMutation({
    mutationFn: (name: string) => writeName(row.parent_id, name),
    onSuccess: (results) => {
      const byId = new Map(results.map((result) => [result.parent_id, result.name]))
      client.setQueryData<Person[]>(PEOPLE_QUERY_KEY, (current) =>
        current?.map((person) => {
          const name = byId.get(person.parent_id)
          return name === undefined ? person : { ...person, name, search: searchText({ ...person, name }) }
        }),
      )
      setDraft(null)
      onRefresh()
    },
  })
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") setDraft(null)
    if (event.key === "Enter") confirm()
  }
  const confirm = () => {
    const name = (draft ?? "").trim()
    if (!name || name === row.name) setDraft(null)
    else save.mutate(name)
  }
  if (draft === null && !save.isPending)
    return (
      <h2 className="flex items-center gap-2">
        <span className="min-w-0 truncate">{row.name}</span>
        <button
          type="button"
          className="name-pencil"
          aria-label="Rename"
          title="Rename"
          disabled={row.in_progress}
          onClick={() => setDraft(row.name)}
        >
          <Pencil className="size-3.5" aria-hidden />
        </button>
      </h2>
    )
  return (
    <h2 className="flex items-center gap-2">
      <input
        ref={field}
        className="name-edit"
        aria-label="Name"
        value={draft ?? row.name}
        disabled={save.isPending}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        type="button"
        className="name-confirm"
        aria-label="Save name"
        disabled={save.isPending}
        onClick={confirm}
      >
        <Check className="size-3.5" aria-hidden />
      </button>
      <button
        type="button"
        className="name-confirm"
        aria-label="Cancel"
        disabled={save.isPending}
        onClick={() => setDraft(null)}
      >
        <X className="size-3.5" aria-hidden />
      </button>
      {save.error ? (
        <span role="alert" className="sub text-destructive">
          {save.error.message}
        </span>
      ) : null}
    </h2>
  )
}

// The detail's strings are "" when it has none; then the row's stand in.
const orNone = (value: string | undefined) => (value === "" ? undefined : value)

// The pills an address or number sits beside: emails by Gmail's, numbers by iMessage's and WhatsApp's.
const EMAIL_PILLS: readonly Channel[] = ["gmail"]
const PHONE_PILLS: readonly Channel[] = ["imessage", "whatsapp"]

// One kind of contact: its pills, the first value, and +N that shows the rest on hover; a click
// pins them open so they can be selected and copied.
function Contact({ pills, values }: { pills: readonly Channel[]; values: readonly string[] }) {
  const [pinned, setPinned] = useState(false)
  const [first, ...rest] = values
  if (!pills.length && first === undefined) return null
  return (
    <span className="relative inline-flex min-w-0 items-center gap-1">
      {pills.map((channel) => (
        <SourcePill key={channel} channel={channel} />
      ))}
      {first}
      {rest.length ? (
        <span className="group inline-flex">
          <button
            type="button"
            aria-expanded={pinned}
            className="h-5 rounded-full border border-line px-1.5 text-[11px] font-semibold tabular-nums hover:text-foreground"
            onClick={() => setPinned(!pinned)}
          >
            +{rest.length}
          </button>
          <Tip side="below" className={pinned ? "pointer-events-auto opacity-100" : undefined}>
            {rest.map((value) => (
              <span key={value} className="block">
                {value}
              </span>
            ))}
          </Tip>
        </span>
      ) : null}
    </span>
  )
}

// "31 interactions · last Nov 2022 · no contact in over 2 years · writes: both": the parts the row
// has, each kept on one line.
function ContactLine({ row }: { row: Person }) {
  const parts = [
    row.interactions ? plural(row.interactions, "interaction") : "",
    row.last_interaction ? `last ${monthYear(row.last_interaction)}` : "",
    row.cadence ? label("cadence", row.cadence).toLowerCase() : "",
    row.direction ? `writes: ${label("direction", row.direction).toLowerCase()}` : "",
  ].filter(Boolean)
  if (!parts.length) return null
  return (
    <div className="sub">
      {parts.map((part, position) => (
        <Fragment key={part}>
          {position ? " · " : null}
          <span className="whitespace-nowrap">{part}</span>
        </Fragment>
      ))}
    </div>
  )
}

// Who this is and how to reach them: the row draws it at once; the detail adds the headline,
// picture, LinkedIn link, and the emails and numbers.
export function DrawerHeader({ row, detail, action, onClose, onRefresh }: DrawerHeaderProps) {
  const avatar = orNone(detail?.avatar_url) ?? (row.avatar_url || undefined)
  const headline = orNone(detail?.headline) ?? [row.title, row.company].filter(Boolean).join(" · ")
  const channels = toChannels(row.channels)
  return (
    <div className="drawer-top person-top">
      <ProxiedLinkedinAvatar name={row.name} size={40} src={avatar} />
      <div className="who">
        <div className="name-row">
          <Name row={row} onRefresh={onRefresh} />
          {detail?.linkedin_url ? <LinkedinLink url={detail.linkedin_url} /> : null}
        </div>
        {headline ? <div className="sub">{headline}</div> : null}
        {row.location ? <div className="sub">{row.location}</div> : null}
        <div className="sub sources">
          <Contact
            pills={channels.filter((channel) => EMAIL_PILLS.includes(channel))}
            values={detail?.emails ?? []}
          />
          <Contact
            pills={channels.filter((channel) => PHONE_PILLS.includes(channel))}
            values={detail?.phones ?? []}
          />
          <Contact
            pills={channels.filter(
              (channel) => !EMAIL_PILLS.includes(channel) && !PHONE_PILLS.includes(channel),
            )}
            values={[]}
          />
        </div>
        <ContactLine row={row} />
      </div>
      <span className="drawer-top-actions">
        {action}
        <DrawerClose onClose={onClose} />
      </span>
    </div>
  )
}
