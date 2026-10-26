import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { useNavigate } from "react-router-dom"

import { ACCOUNT_KEY, fetchAccount } from "@/lib/api/account"
import { errorText } from "@/lib/api/http"
import { continueSetup } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"
import { Connect } from "@/pages/install/Connect"

import { Frame, Heading, Info, Note } from "./Frame"
import { Guide } from "./guide/Guide"
import type { GuideSource } from "./guide/demos"
import { STEPS } from "./steps"

/** Sources: the other sign-ins, each a row of the Connect list (Messages is the Full Disk Access
 *  switch, Gmail and WhatsApp their sign-ins). Done starts setup over every connected source (their
 *  imports, then deep context once, then the index) and opens Set up on its progress; the app is one
 *  click from there. The right column shows what each source adds until a source's icon or Connect
 *  is clicked: then its guide, what the person is about to see, with their own name in it. */
export function Sources() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const [guide, setGuide] = useState<GuideSource | null>(null)
  const account = useQuery({ queryKey: ACCOUNT_KEY, queryFn: ({ signal }) => fetchAccount(signal) })
  const person = { name: account.data?.name ?? "You", email: account.data?.email ?? "" }
  const setup = STEPS.find((step) => step.key === "setup")
  const done = () => {
    setError(null)
    continueSetup({})
      .then(() => setup && void navigate(setup.href, { state: { startedAt: Date.now() } }))
      .catch((caught: unknown) => setError(errorText(caught)))
  }
  return (
    <Frame
      step="sources"
      left={
        <>
          <Heading title="Add more sources" line="Each one adds people; nothing already done is redone." />
          {isDesktop() ? (
            <Connect onDone={done} rows={["imessage", "gmail", "whatsapp"]} onGuide={setGuide} />
          ) : (
            <Note>The sign-ins run in the Powerpacks app; this browser view has no app to run them.</Note>
          )}
          {error ? <Note bad>{error}</Note> : null}
        </>
      }
      right={
        guide ? (
          <Guide key={guide} source={guide} person={person} />
        ) : (
          <Info
            title="What each one adds, and how long"
            lines={[
              "Messages: the people you text. One macOS switch; a few minutes to read.",
              "Gmail: everyone you email. Sign in once; the first sync takes minutes to an hour, by mailbox size.",
              "WhatsApp: scan a code with your phone. The first sync takes 30 minutes to a few hours.",
            ]}
            note="Done starts them together, then learns who everyone is. It runs in the background; you can use Powerpacks meanwhile."
          />
        )
      }
    />
  )
}
