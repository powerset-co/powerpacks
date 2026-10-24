import { useNavigate } from "react-router-dom"

import { isDesktop } from "@/lib/desktop"
import { HOME } from "@/lib/nav"
import { Connect } from "@/pages/install/Connect"

import { Frame, Heading, Info, Note } from "./Frame"

/** Sources: the other sign-ins, each a row of the Connect list (Messages is the Full Disk Access
 *  switch, Gmail and WhatsApp their sign-ins). Done leaves onboarding for the app. */
export function Sources() {
  const navigate = useNavigate()
  return (
    <Frame
      step="sources"
      skip={false}
      left={
        <>
          <Heading title="Add more sources" line="Each one adds people; nothing is redone." />
          {isDesktop() ? (
            <Connect onDone={() => void navigate(HOME.href)} />
          ) : (
            <Note>The sign-ins run in the Powerpacks app; this browser view has no app to run them.</Note>
          )}
        </>
      }
      right={
        <Info
          title="Why connect more"
          lines={[
            "Messages: the people you actually text. No sign-in, one macOS switch.",
            "Gmail: everyone you email.",
            "WhatsApp: scan a code with your phone.",
          ]}
          note="Powerset and Codex are connected by your account."
        />
      }
    />
  )
}
