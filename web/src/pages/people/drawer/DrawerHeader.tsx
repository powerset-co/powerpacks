import { Avatar, DrawerClose, LinkedinLink, SourcePills } from "@/components/shared"
import { avatarUrl } from "@/lib/api/people"
import { toChannels, type Channel } from "@/lib/channels"
import type { Person, PersonDetail } from "@/types/people"

interface DrawerHeaderProps {
  row: Person
  detail: PersonDetail | null
  onClose: () => void
}

// The detail's strings are "" when it has none; then the row's stand in.
const orNone = (value: string | undefined) => (value === "" ? undefined : value)

// Hovering a source pill says which address or number the family was found by.
function sourceTitles(detail: PersonDetail | null): Partial<Record<Channel, string>> {
  if (!detail) return {}
  const emails = detail.emails.join(", ")
  const phones = detail.phones.join(", ")
  return { gmail: emails || undefined, imessage: phones || undefined, whatsapp: phones || undefined }
}

// Who this is: the row draws it at once; the detail adds the headline, picture and LinkedIn link.
export function DrawerHeader({ row, detail, onClose }: DrawerHeaderProps) {
  const avatar = orNone(detail?.avatar_url) ?? (row.has_avatar ? avatarUrl(row.parent_id) : undefined)
  const headline = orNone(detail?.headline) ?? [row.title, row.company].filter(Boolean).join(" · ")
  return (
    <div className="drawer-top">
      <Avatar name={row.name} size={40} src={avatar} />
      <div className="who">
        <div className="name-row">
          <h2>{row.name}</h2>
          {detail?.linkedin_url ? <LinkedinLink url={detail.linkedin_url} /> : null}
        </div>
        {headline ? <div className="sub">{headline}</div> : null}
        {row.location ? <div className="sub">{row.location}</div> : null}
        <div className="sub sources">
          <SourcePills channels={toChannels(row.channels)} titles={sourceTitles(detail)} />
        </div>
      </div>
      <DrawerClose onClose={onClose} />
    </div>
  )
}
