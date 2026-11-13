import { useState } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchMutuals, type MutualsAnswer } from "@/lib/api/linkedin"

import { OperatorInitials } from "./Operators"

// The drawer's Get Mutuals: on click, the people this person and the user both know on LinkedIn
// (the count and the first ten). The drawer remounts it per person, so nothing carries over.
export function Mutuals({ linkedinUrl }: { linkedinUrl: string }) {
  const [state, setState] = useState<"idle" | "loading" | MutualsAnswer | Error>("idle")
  if (!linkedinUrl) return null
  const get = () => {
    setState("loading")
    fetchMutuals(linkedinUrl).then(setState, (error: unknown) => setState(new Error(errorText(error))))
  }
  return (
    <section className="review-sections" data-mutuals>
      <h4>Mutual connections</h4>
      {state === "idle" || state === "loading" ? (
        <div>
          <Button size="lg" disabled={state === "loading"} onClick={get}>
            {state === "loading" ? "Getting mutuals…" : "Get Mutuals"}
          </Button>
        </div>
      ) : (
        <MutualsList answer={state} />
      )}
    </section>
  )
}

function MutualsList({ answer }: { answer: MutualsAnswer | Error }) {
  if (answer instanceof Error) return <p>{answer.message}</p>
  if (answer.status === "connected") return <p>You're connected.</p>
  if (answer.status === "over_budget")
    return <p>Today's {answer.day_cap} LinkedIn lookups are used up. Try again tomorrow.</p>
  if (answer.error || answer.count == null)
    return <p>{answer.error ?? "LinkedIn didn't answer. Try again later."}</p>
  const people = answer.people ?? []
  if (!answer.count) return <p>No mutual connections.</p>
  return (
    <>
      <p>
        {answer.count.toLocaleString("en-US")} mutual connection{answer.count === 1 ? "" : "s"}
      </p>
      <ul className="evidence-operators">
        {people.map((person) => (
          <li key={person.url || person.name}>
            <OperatorInitials name={person.name} />
            <span>
              {person.url ? (
                <a href={person.url} target="_blank" rel="noreferrer">
                  <b>{person.name}</b>
                </a>
              ) : (
                <b>{person.name}</b>
              )}
              {person.headline ? <small>{person.headline}</small> : null}
            </span>
          </li>
        ))}
      </ul>
    </>
  )
}
