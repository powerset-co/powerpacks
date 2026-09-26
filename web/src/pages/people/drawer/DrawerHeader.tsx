import { Avatar, SourcePills } from "@/components/shared";
import { avatarUrl } from "@/lib/api/people";
import type { Person, PersonDetail } from "@/types/people";

import { toChannels } from "../channels";

interface DrawerHeaderProps {
  row: Person;
  detail: PersonDetail | null;
  onClose: () => void;
}

// Who this is: the row draws it at once; the detail adds the headline, picture and LinkedIn link.
export function DrawerHeader({ row, detail, onClose }: DrawerHeaderProps) {
  const avatar = detail?.avatar_url || (row.has_avatar ? avatarUrl(row.parent_id) : undefined);
  const headline = detail?.headline || [row.title, row.company].filter(Boolean).join(" · ");
  return (
    <div className="drawer-top">
      <Avatar name={row.name} size={40} src={avatar} />
      <div className="who">
        <h2>{row.name}</h2>
        {headline ? <div className="sub">{headline}</div> : null}
        {row.location ? <div className="sub">{row.location}</div> : null}
        <div className="sub sources">
          <SourcePills channels={toChannels(row.channels)} />
          {detail?.linkedin_url ? (
            <a href={detail.linkedin_url} target="_blank" rel="noreferrer">View LinkedIn profile</a>
          ) : null}
        </div>
      </div>
      <button type="button" className="drawer-close" data-drawer-close aria-label="Close details" onClick={onClose}>×</button>
    </div>
  );
}
