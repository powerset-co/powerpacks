import { Fragment, useState } from "react"

import type { LogbookConversation, LogbookConversationBody } from "@/lib/api/logbook"
import { LOGBOOK } from "@/lib/people/copy"
import { messageSenders, participantRoles } from "@/lib/people/logbook"

// Names shown per role before "Show all".
const SHOWN = 6

interface Line {
  role: string
  names: { name: string; title?: string }[]
}

/** The lines to show: a Gmail thread's From / To / Cc / Bcc from the mail store; else, for a
 *  thread or group, only who wrote in the saved messages, called Senders (nothing about
 *  recipients is known); a direct message's people are already its title. */
function lines(conversation: LogbookConversation, body: LogbookConversationBody): Line[] {
  if (body.participants) {
    return participantRoles(body.participants).map(({ role, people }) => ({
      role,
      names: people.map((person) => ({
        name: person.name && person.name !== person.email ? `${person.name} <${person.email}>` : person.email,
      })),
    }))
  }
  if (conversation.kind === "dm") return []
  return [{ role: LOGBOOK.senders, names: messageSenders(body.messages).map((name) => ({ name })) }]
}

// Who is on the conversation, as far as the saved data says.
export function People({
  conversation,
  body,
}: {
  conversation: LogbookConversation
  body: LogbookConversationBody
}) {
  const [all, setAll] = useState(false)
  const shown = lines(conversation, body)
  if (!shown.length) return null
  const long = shown.some(({ names }) => names.length > SHOWN)
  return (
    <dl className="logbook-people" data-participants>
      {shown.map(({ role, names }) => (
        <div key={role}>
          <dt>{role}</dt>
          <dd>
            {(all ? names : names.slice(0, SHOWN)).map(({ name, title }, position) => (
              <Fragment key={`${name}-${position}`}>
                {position ? ", " : null}
                <span title={title}>{name}</span>
              </Fragment>
            ))}
            {!all && names.length > SHOWN ? (
              <span className="logbook-people-more"> +{(names.length - SHOWN).toLocaleString()}</span>
            ) : null}
          </dd>
        </div>
      ))}
      {long ? (
        <button type="button" className="logbook-people-all" onClick={() => setAll(!all)}>
          {all ? LOGBOOK.fewer : LOGBOOK.everyone}
        </button>
      ) : null}
    </dl>
  )
}
