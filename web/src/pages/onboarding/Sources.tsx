import { useState } from "react"
import { useNavigate } from "react-router-dom"

import { errorText } from "@/lib/api/http"
import { continueSetup } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"
import { Connect } from "@/pages/install/Connect"

import { Frame, Heading, Info, Note } from "./Frame"
import { STEPS } from "./steps"

/** Sources: the other sign-ins, each a row of the Connect list (Messages is the Full Disk Access
 *  switch, Gmail and WhatsApp their sign-ins). Done starts setup over every connected source (their
 *  imports, then deep context once, then the index) and opens Set up on its progress; the app is one
 *  click from there. */
export function Sources() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
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
            <Connect onDone={done} rows={["imessage", "gmail", "whatsapp"]} />
          ) : (
            <Note>The sign-ins run in the Powerpacks app; this browser view has no app to run them.</Note>
          )}
          {error ? <Note bad>{error}</Note> : null}
        </>
      }
      right={
        <Info
          title="What each one adds, and how long"
          lines={[
            "Messages: the people you text. One macOS switch; a few minutes to read.",
            "Gmail: everyone you email. Sign in once; the first sync takes minutes to an hour, by mailbox size.",
            "WhatsApp: scan a code with your phone. The first sync takes 30 minutes to a few hours.",
          ]}
          note="Done starts them together, then learns who everyone is. It runs in the background; you can use Powerpacks meanwhile."
        />
      }
    />
  )
}
