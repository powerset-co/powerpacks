import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"

import { Avatar } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchInstall } from "@/lib/api/install"
import { fetchPeople } from "@/lib/api/people"
import { HOME } from "@/lib/nav"
import type { Person } from "@/types/people"

import { Frame, Heading, Note } from "./Frame"
import { after } from "./steps"

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

/** First look: a few people the person knows, three questions to try, and the rest still being
 *  processed. Onboarding ends here; Sources is the way to add more. */
export function FirstLook() {
  const navigate = useNavigate()
  const people = useQuery({ queryKey: ["people"], queryFn: fetchPeople, retry: false })
  const install = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    retry: false,
  })
  const shown = recognizable(people.data ?? [])
  const total = people.data?.length ?? 0
  const processing = install.data !== undefined && install.data.event !== "search.ready"
  const next = after("first-look")
  return (
    <Frame
      step="first-look"
      left={
        <>
          <Heading title="People you know" />
          {people.error ? <Note bad>{errorText(people.error)}</Note> : null}
          {people.isPending ? <Note>Reading your people…</Note> : null}
          {people.data && shown.length === 0 ? (
            <Note>No one with a title and a company yet; give it a minute.</Note>
          ) : null}
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {shown.map((person) => (
              <li
                key={person.parent_id}
                className="flex items-center gap-3 rounded-[var(--radius-m)] border border-line-strong bg-card px-3 py-2.5"
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
          {total > 0 ? (
            <Note>
              {total.toLocaleString()} people so far
              {processing ? " · processing continues in the background" : ""}
            </Note>
          ) : null}
          <button
            type="button"
            className="cursor-pointer self-start border-0 bg-transparent p-0 text-xs text-muted-foreground underline hover:text-foreground"
            onClick={() => void navigate(HOME.href)}
          >
            See everyone so far
          </button>
        </>
      }
      right={
        <>
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
          <Note>Connect Messages to add the people you text.</Note>
          <div className="flex gap-2">
            <Button variant="primary" onClick={() => next && void navigate(next.href)}>
              Add more sources
            </Button>
            <Button onClick={() => void navigate("/agent")}>Start asking</Button>
          </div>
        </>
      }
    />
  )
}
