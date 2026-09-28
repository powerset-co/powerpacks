import { monthYear } from "@/lib/copy"
import type { Education, PondCandidate, Position } from "@/types/searches"

// rendering.py _company_note: size, stage and money raised.
function companyNote(position: Position): string {
  const facts: string[] = []
  if (position.headcount) facts.push(`${position.headcount.toLocaleString()} people`)
  if (position.stage) facts.push(position.stage)
  if (position.funding) {
    facts.push(
      position.funding >= 1e9
        ? `$${(position.funding / 1e9).toFixed(1)}B raised`
        : `$${(position.funding / 1e6).toFixed(0)}M raised`,
    )
  }
  return facts.join(" · ")
}

interface PositionsProps {
  positions: readonly Position[]
  matched: readonly number[]
}

// The drawer's work experience (the matched roles marked) and education.
export function Career({ row }: { row: PondCandidate }) {
  if (!row.positions.length && !row.education.length) return null
  return (
    <div className="review-sections" data-career>
      {row.positions.length ? (
        <section>
          <h4>Work experience</h4>
          <Positions positions={row.positions} matched={row.matched_positions} />
        </section>
      ) : null}
      {row.education.length ? (
        <section>
          <h4>Education</h4>
          <Schools education={row.education} />
        </section>
      ) : null}
    </div>
  )
}

// rendering.py _position_item: the roles the search matched on are marked.
function Positions({ positions, matched }: PositionsProps) {
  return (
    <ol className="career" data-positions>
      {positions.map((position, index) => {
        const note = companyNote(position)
        const end = position.is_current ? "Present" : monthYear(position.end_date)
        return (
          <li
            key={`${position.company}:${position.start_date}:${index}`}
            data-matched={matched.includes(index)}
          >
            <span className="career-head">
              <b>{position.title}</b>
              {matched.includes(index) ? <em className="career-chip">Matched</em> : null}
              {position.is_current ? <em className="career-chip current">Current</em> : null}
            </span>
            <span className="career-org">
              {position.company_url ? (
                <a href={position.company_url} target="_blank" rel="noreferrer">
                  {position.company}
                </a>
              ) : (
                position.company
              )}
              {note ? <small> · {note}</small> : null}
            </span>
            <span className="career-dates">
              {monthYear(position.start_date)} – {end}
            </span>
            {position.description ? <p>{position.description}</p> : null}
          </li>
        )
      })}
    </ol>
  )
}

// rendering.py _education_item.
function Schools({ education }: { education: readonly Education[] }) {
  return (
    <ol className="career">
      {education.map((school, index) => {
        const course = [school.degree, school.field_of_study].filter(Boolean).join(" in ")
        const years =
          school.start_year && school.end_year
            ? `${school.start_year} – ${school.end_year}`
            : String(school.end_year || school.start_year || "")
        return (
          <li key={`${school.school}:${index}`}>
            <span className="career-head">
              <b>{school.school}</b>
            </span>
            {course ? <span className="career-org">{course}</span> : null}
            {years ? <span className="career-dates">{years}</span> : null}
          </li>
        )
      })}
    </ol>
  )
}
