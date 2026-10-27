import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"

import { Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { isDesktop } from "@/lib/desktop"

import { Frame, Heading, Info, Note } from "./Frame"
import { after } from "./steps"

/** Codex: the ChatGPT sign-in the chat runs on, in the browser, the same flow as the Connect
 *  screen's Codex row. A sign-in started here moves the screen on by itself once the account is
 *  read; someone already signed in sees who, Continue, and Switch account. */
export function Codex() {
  const navigate = useNavigate()
  const codex = useCodexAccount()
  const next = after("codex")
  const account = codex.status?.installed ? codex.status.account : null
  const signedIn = account !== null
  const [started, setStarted] = useState(false)
  useEffect(() => {
    if (started && codex.login === null && signedIn && next) void navigate(next.href)
  }, [started, codex.login, signedIn, next, navigate])
  const start = () => {
    setStarted(true)
    codex.connect()
  }
  const switchAccount = () => {
    setStarted(true)
    codex.signOut()
    codex.connect()
  }
  const who = account?.kind === "chatgpt" && account.email ? account.email : "your ChatGPT account"
  return (
    <Frame
      step="codex"
      left={
        <>
          <Heading title="Sign in to ChatGPT" line="Powerpacks chats on your ChatGPT plan." />
          {!isDesktop() ? (
            <Note>The sign-in runs in the Powerpacks app; this browser view has no app to run it.</Note>
          ) : signedIn ? (
            <>
              <Note>
                Signed in as {who}.{" "}
                <button type="button" className={LINK} onClick={switchAccount}>
                  Switch account
                </button>
              </Note>
              <Button variant="primary" size="lg" onClick={() => next && void navigate(next.href)}>
                Continue
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="primary"
                size="lg"
                disabled={codex.loading || codex.login !== null || codex.status?.installed === false}
                onClick={start}
              >
                {codex.login ? (
                  <>
                    <Spinner /> Finish signing in in your browser
                  </>
                ) : (
                  "Sign in via browser ↗"
                )}
              </Button>
              {codex.login ? (
                <Note>
                  No window?{" "}
                  <button type="button" className={LINK} onClick={codex.openBrowser}>
                    Open it again
                  </button>
                </Note>
              ) : (
                <Note>Any ChatGPT plan works: Free, Plus or Pro.</Note>
              )}
            </>
          )}
          {codex.error ? <Note bad>{codex.error}</Note> : null}
        </>
      }
      right={
        <Info
          title="Why ChatGPT"
          lines={[
            "Questions about your network are answered by Codex, OpenAI's agent, on your own plan.",
            "Your network stays on this computer; the chat reads it here.",
            "Sign in once; Powerpacks keeps the session.",
          ]}
          note="Next: LinkedIn, then a first look at people you know."
        />
      }
    />
  )
}

const LINK = "cursor-pointer border-0 bg-transparent p-0 text-xs text-primary hover:underline"
