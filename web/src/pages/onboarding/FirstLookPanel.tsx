import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { useNavigate } from "react-router-dom"

import { Avatar, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchFirstLookAnswer, type FirstLook, type FirstLookPerson } from "@/lib/api/install"
import { cn } from "@/lib/utils"

import { Note } from "./Frame"
import { STEPS } from "./steps"

const SHOWN = 3
const CHIP =
  "w-full cursor-pointer rounded-full border border-line-strong bg-surface-2 px-4 py-2 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] disabled:cursor-default"
const LINK =
  "cursor-pointer border-0 bg-transparent p-0 text-xs text-muted-foreground underline hover:text-foreground"

/** The first few people with a title and a company: the person recognizes them at a glance. */
function recognizable(people: readonly FirstLookPerson[]): FirstLookPerson[] {
  return people.filter((person) => person.title && person.company).slice(0, SHOWN)
}

function People({ people }: { people: readonly FirstLookPerson[] }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {people.map((person) => (
        <li
          key={person.public_identifier || person.name}
          className="rise-in flex items-center gap-3 rounded-[var(--radius-m)] border border-line-strong bg-background px-3 py-2.5"
        >
          <Avatar name={person.name} src={person.avatar_url || null} className="size-8 text-xs" />
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-[13px] font-bold">{person.name}</span>
            <span className="truncate text-xs text-muted-foreground">
              {[person.title, person.company].filter(Boolean).join(" · ")}
            </span>
          </span>
        </li>
      ))}
    </ul>
  )
}

/** The first look, in the Set up screen's right column once people are in: a few people the
 *  person knows and the questions the index can answer, answered right here. Nothing leaves the
 *  onboarding: the one way on is Add more sources. Rises in over the page picture. */
export function FirstLookPanel({ look, ready }: { look: FirstLook; ready: boolean }) {
  const navigate = useNavigate()
  const [asked, setAsked] = useState<string | null>(null)
  const answer = useQuery({
    queryKey: ["onboarding", "first-look", asked],
    queryFn: ({ signal }) => fetchFirstLookAnswer(asked ?? "", signal),
    enabled: asked !== null,
    retry: false,
  })
  const shown = recognizable(look.people)
  const sources = STEPS.find((step) => step.key === "sources")
  const answered = asked !== null && answer.data !== undefined
  return (
    <div className="rise-in flex flex-col gap-5" data-first-look>
      <div key={answered ? asked : "people"} className="rise-in flex flex-col gap-3">
        <h2 className="m-0 flex items-center gap-2 text-[15px] font-semibold">
          {answered ? answer.data.question : "People you know"}
          {asked !== null && answer.isFetching ? <Spinner className="size-3.5 text-primary" /> : null}
        </h2>
        {answered ? (
          <>
            <People people={answer.data.people} />
            <Note>
              {answer.data.total > answer.data.people.length
                ? `${answer.data.people.length} of ${answer.data.total.toLocaleString()} shown · `
                : `${answer.data.total.toLocaleString()} people · `}
              <button type="button" className={LINK} onClick={() => setAsked(null)}>
                Back to people you know
              </button>
            </Note>
          </>
        ) : (
          <>
            {shown.length === 0 ? (
              <Note>No one with a title and a company yet; give it a minute.</Note>
            ) : null}
            <People people={shown} />
            <Note>
              {look.total.toLocaleString()} people{" "}
              {ready ? "searchable" : "so far · the rest is still being built"}
            </Note>
          </>
        )}
        {answer.error ? <Note bad>{errorText(answer.error)}</Note> : null}
      </div>
      {look.questions.length > 0 ? (
        <div className="flex flex-col gap-3">
          <h2 className="m-0 text-[15px] font-semibold">Try asking</h2>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {look.questions.map((question) => (
              <li key={question.key}>
                <button
                  type="button"
                  className={cn(CHIP, asked === question.key && "border-primary")}
                  aria-pressed={asked === question.key}
                  disabled={answer.isFetching}
                  onClick={() => setAsked(question.key)}
                >
                  {question.text}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => sources && void navigate(sources.href)}>
          Add more sources
        </Button>
      </div>
    </div>
  )
}
