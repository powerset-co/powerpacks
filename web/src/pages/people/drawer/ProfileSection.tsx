import { FactList } from "@/components/shared"
import type { PersonDetail } from "@/types/people"

import { Pair } from "./Pair"

// The imported LinkedIn profile as the review cards draw it: the About text, then Location, Work and
// Education. A LinkedIn-only person has nothing else to show.
export function ProfileSection({ detail }: { detail: PersonDetail }) {
  return (
    <div className="dsec profile">
      {detail.summary ? <p>{detail.summary}</p> : null}
      <dl className="kv">
        <Pair term="Location">{detail.location}</Pair>
        <Pair term="Work">{detail.experiences.length ? <FactList items={detail.experiences} /> : null}</Pair>
        <Pair term="Education">{detail.education.length ? <FactList items={detail.education} /> : null}</Pair>
      </dl>
    </div>
  )
}
