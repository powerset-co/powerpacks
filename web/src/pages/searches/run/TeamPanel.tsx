import { useState } from "react"

import { initials } from "@/components/shared"
import { usePresence } from "@/hooks/usePresence"
import { monthYear, runDate } from "@/lib/searches/copy"
import type { SearchResult } from "@/types/searches"

// rendering.py _team_table: the company's current employees as saved, under the results.
// The header folds the list open; the list fades in and out.
export function TeamPanel({ search }: { search: SearchResult }) {
  const [open, setOpen] = useState(false)
  const body = usePresence(open ? search.team : null)
  const status = search.team_status ? (
    <p className="team-status">Team similarity: {search.team_status}</p>
  ) : null
  if (!search.team.length) return status
  return (
    <section className="team" data-team data-open={open}>
      <button type="button" className="team-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        Team <span>{search.team.length.toLocaleString()}</span>
        <small>Current employees · Saved {runDate(search.team_fetched_at)}</small>
      </button>
      {body.mounted ? (
        <div className="team-body" data-open={body.open} onTransitionEnd={body.onTransitionEnd}>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Title</th>
                <th>Location</th>
                <th>Tenure</th>
              </tr>
            </thead>
            <tbody>
              {search.team.map((member, index) => (
                <tr key={`${member.linkedin_url || member.name}:${index}`} data-team-row>
                  <td>
                    <span className="team-name">
                      <span className="operator-initials" aria-hidden="true">
                        {initials(member.name)}
                      </span>
                      {member.linkedin_url ? (
                        <a href={member.linkedin_url} target="_blank" rel="noreferrer">
                          {member.name || "Name unavailable"}
                        </a>
                      ) : (
                        member.name || "Name unavailable"
                      )}
                    </span>
                  </td>
                  <td>{member.title}</td>
                  <td>{member.location}</td>
                  <td>{member.started_on ? `${monthYear(member.started_on)} – Present` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {status}
    </section>
  )
}
