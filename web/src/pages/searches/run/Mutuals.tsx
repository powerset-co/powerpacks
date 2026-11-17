import { ProxiedLinkedinAvatar } from "@/components/shared"
import { LinkedInIcon } from "@/components/shared/icons/channels"
import type { MutualsAnswer } from "@/lib/api/linkedin"

import type { Mutuals } from "../hooks/useMutuals"

const STACKED = 3

/** What the lookup said, in words: the count, or why there is none. */
function words(answer: MutualsAnswer | Error): string {
  if (answer instanceof Error) return answer.message
  if (answer.status === "connected") return "You're connected"
  if (answer.status === "over_budget")
    return `Today's ${answer.day_cap} LinkedIn lookups are used up. Try again tomorrow.`
  if (answer.error || answer.count == null) return answer.error ?? "LinkedIn didn't answer. Try again later."
  if (!answer.count) return "No mutual connections"
  return `${answer.count.toLocaleString("en-US")} mutual connection${answer.count === 1 ? "" : "s"}`
}

const peopleOf = (state: Mutuals["state"]) =>
  typeof state === "string" || state instanceof Error ? [] : (state.people ?? [])

// The line under the person's location: the Get mutuals link, then the count with the first faces.
export function MutualsLine({ mutuals }: { mutuals: Mutuals }) {
  const { state, get } = mutuals
  if (state === "idle" || state === "loading")
    return (
      <button
        type="button"
        className="mutuals-line"
        data-mutuals
        disabled={state === "loading"}
        onClick={get}
      >
        <LinkedInIcon />
        {state === "loading" ? "Getting mutuals…" : "Get mutuals"}
      </button>
    )
  return (
    <div className="mutuals-line" data-mutuals>
      <span className="mutuals-stack">
        {peopleOf(state)
          .slice(0, STACKED)
          .map((person) => (
            <ProxiedLinkedinAvatar key={person.url || person.name} name={person.name} size={26} />
          ))}
      </span>
      {words(state)}
    </div>
  )
}

// Under the drawer's header once the lookup answered with people: the first ten, each to their profile.
export function MutualsSection({ mutuals }: { mutuals: Mutuals }) {
  const people = peopleOf(mutuals.state)
  if (!people.length) return null
  return (
    <section className="review-sections" data-mutuals-list>
      <h4>Mutual connections</h4>
      <ul className="mutuals-list">
        {people.map((person) => (
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
    </section>
  )
}
