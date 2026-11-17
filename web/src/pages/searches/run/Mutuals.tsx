import { useState } from "react"

import { ProxiedLinkedinAvatar } from "@/components/shared"
import { LinkedInIcon } from "@/components/shared/icons/channels"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchMutuals, type MutualsAnswer } from "@/lib/api/linkedin"

// The drawer's Get Mutuals: a small button beside the heading; on click, the count takes its place
// and the first ten people this person and the user both know on LinkedIn list under it. The drawer remounts it per person, so nothing carries over.
export function Mutuals({ linkedinUrl }: { linkedinUrl: string }) {
  const [state, setState] = useState<"idle" | "loading" | MutualsAnswer | Error>("idle")
  if (!linkedinUrl) return null
  const get = () => {
    setState("loading")
    fetchMutuals(linkedinUrl).then(setState, (error: unknown) => setState(new Error(errorText(error))))
  }
  const answered = state !== "idle" && state !== "loading"
  const count = answered && !(state instanceof Error) && !state.error ? state.count : null
  return (
    <section className="review-sections" data-mutuals>
      <div className="mutuals-head">
        <h4>Mutual connections</h4>
        {answered ? (
          count ? (
            <span className="mutuals-count">{count.toLocaleString("en-US")}</span>
          ) : null
        ) : (
          <Button variant="outline" size="sm" disabled={state === "loading"} onClick={get}>
            <LinkedInIcon />
            {state === "loading" ? "Getting mutuals…" : "Get Mutuals"}
          </Button>
        )}
      </div>
      {answered ? <MutualsList answer={state} /> : null}
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
  if (!answer.count) return <p>No mutual connections.</p>
  return (
    <ul className="mutuals-list">
      {(answer.people ?? []).map((person) => (
        <li key={person.url || person.name}>
          <ProxiedLinkedinAvatar name={person.name} size={26} />
          <span>
            {person.url ? (
              <a href={person.url} target="_blank" rel="noreferrer">
                {person.name}
              </a>
            ) : (
              <b>{person.name}</b>
            )}
            {person.headline ? <small>{person.headline}</small> : null}
          </span>
        </li>
      ))}
    </ul>
  )
}
