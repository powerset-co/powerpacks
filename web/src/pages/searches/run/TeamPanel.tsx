import { useState } from "react"

import { DetailsSection } from "@/components/shared"
import { monthYear } from "@/lib/copy"
import { runDate } from "@/lib/searches/copy"
import type { SearchResult } from "@/types/searches"

import { OperatorInitials } from "./Operators"

// rendering.py _team_table: the company's current employees as saved, folded under the job
// description in the run's header.
export function TeamPanel({ search }: { search: SearchResult }) {
  const [open, setOpen] = useState(false)
  const status = search.team_status ? (
    <p className="team-status">Team similarity: {search.team_status}</p>
  ) : null
  if (!search.team.length) return status
  return (
    <div className="team" data-team>
      <DetailsSection
        sectionKey="team"
        title="Team"
        count={search.team.length}
        open={open}
        onToggle={setOpen}
      >
        <div className="team-body">
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
                      <OperatorInitials name={member.name} />
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
          <p className="team-saved">Current employees · Saved {runDate(search.team_fetched_at)}</p>
        </div>
      </DetailsSection>
      {status}
    </div>
  )
}
