import { useNavigate } from "react-router-dom"

import { Avatar } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { HOME } from "@/lib/nav"
import type { Person } from "@/types/people"

import { Note } from "./Frame"
import { STEPS } from "./steps"

const SHOWN = 3
const QUESTIONS = [
  "Who do I know at an early-stage fintech?",
  "Who did I lose touch with this year?",
  "Who can introduce me to a CTO in Berlin?",
]
const CHIP =
  "w-full cursor-pointer rounded-full border border-line-strong bg-surface-2 px-4 py-2 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"

/** The first few people with a title and a company: the person recognizes them at a glance. */
function recognizable(people: readonly Person[]): Person[] {
  return people.filter((person) => !person.is_owner && person.title && person.company).slice(0, SHOWN)
}

/** The first look, in the Set up screen's right column once people are in: a few people the
 *  person knows, three questions to try, and where to go next. Rises in over the page picture. */
export function FirstLookPanel({ people, ready }: { people: readonly Person[]; ready: boolean }) {
  const navigate = useNavigate()
  const shown = recognizable(people)
  const sources = STEPS.find((step) => step.key === "sources")
  return (
    <div className="rise-in flex flex-col gap-5" data-first-look>
      <div className="flex flex-col gap-3">
        <h2 className="m-0 text-[15px] font-semibold">People you know</h2>
        {shown.length === 0 ? <Note>No one with a title and a company yet; give it a minute.</Note> : null}
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {shown.map((person) => (
            <li
              key={person.parent_id}
              className="rise-in flex items-center gap-3 rounded-[var(--radius-m)] border border-line-strong bg-background px-3 py-2.5"
            >
              <Avatar name={person.name} src={person.avatar_url || null} className="size-8 text-xs" />
              <span className="flex min-w-0 flex-col">
                <span className="truncate text-[13px] font-bold">{person.name}</span>
                <span className="truncate text-xs text-muted-foreground">
                  {person.title} · {person.company}
                </span>
              </span>
            </li>
          ))}
        </ul>
        <Note>
          {people.length.toLocaleString()} people{" "}
          {ready ? "searchable" : "so far · the rest is still being built"}
        </Note>
      </div>
      <div className="flex flex-col gap-3">
        <h2 className="m-0 text-[15px] font-semibold">Try asking</h2>
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {QUESTIONS.map((question) => (
            <li key={question}>
              <button type="button" className={CHIP} onClick={() => void navigate("/agent")}>
                {question}
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => void navigate("/agent")}>
          Start asking
        </Button>
        <Button onClick={() => sources && void navigate(sources.href)}>Add more sources</Button>
        <Button variant="ghost" onClick={() => void navigate(HOME.href)}>
          See everyone
        </Button>
      </div>
    </div>
  )
}
