import { useEffect, useRef } from "react"
import { useNavigate } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { useCodexAccount, type CodexAccountState } from "@/lib/agent/useCodexAccount"
import { isDesktop } from "@/lib/desktop"
import { ChatGptSignIn } from "@/components/shared"

import { Frame, Heading, Info, Note } from "./Frame"
import { after } from "./steps"

// Where someone without a ChatGPT account makes one. Opens in the system browser; the
// referral code rides on this URL once there is one.
const SIGN_UP_URL = "https://chatgpt.com/auth/login"

/** The one line under the heading: who is signed in, or why the button is there. */
function line(codex: CodexAccountState): string {
  if (codex.loading) return "Checking ChatGPT…"
  if (!codex.status) return "Could not connect to Codex."
  if (!codex.status.installed) return "Codex is missing from this app."
  const account = codex.status.account
  if (account === null) return "The chat runs on your ChatGPT plan."
  if (account.kind === "chatgpt") return `Signed in as ${account.email ?? "ChatGPT"}.`
  return account.kind === "apiKey" ? "Signed in with an API key." : "Signed in."
}

/** Codex: the ChatGPT sign-in the chat runs on. One line says who is signed in, one button
 *  connects or continues. A sign-in finished here moves the screen on by itself; someone
 *  already signed in sees who they are and Continue. */
export function Codex() {
  const navigate = useNavigate()
  const codex = useCodexAccount()
  const next = after("codex")
  const signedIn = codex.status?.installed === true && codex.status.account !== null
  // A sign-in ran on this screen: once its account is read, the screen moves on.
  const started = useRef(false)
  useEffect(() => {
    if (codex.login) started.current = true
    if (started.current && codex.login === null && signedIn && next) void navigate(next.href)
  }, [codex.login, signedIn, next, navigate])
  return (
    <Frame
      step="codex"
      left={
        <>
          <Heading title="Sign in to ChatGPT" line={line(codex)} />
          {isDesktop() ? (
            <>
              {codex.login ? (
                <ChatGptSignIn codex={codex} compact />
              ) : signedIn ? (
                <>
                  <Button variant="primary" size="lg" onClick={() => next && void navigate(next.href)}>
                    Continue
                  </Button>
                  <button
                    type="button"
                    onClick={codex.signOut}
                    className="cursor-pointer self-start border-0 bg-transparent p-0 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                  >
                    Sign out
                  </button>
                </>
              ) : codex.status?.installed ? (
                <Button variant="primary" size="lg" onClick={codex.connect}>
                  Connect ChatGPT
                </Button>
              ) : codex.status || codex.loading ? null : (
                <Button variant="primary" size="lg" onClick={codex.retry}>
                  Try again
                </Button>
              )}
              {codex.error ? <p className="m-0 text-xs text-bad">{codex.error}</p> : null}
            </>
          ) : (
            <Note>The sign-in runs in the Powerpacks app; this browser view has no app to run it.</Note>
          )}
        </>
      }
      right={
        <>
          <Info
            title="Why ChatGPT"
            lines={[
              "Powerpacks chats on your ChatGPT plan: questions about your network are answered by Codex, OpenAI's agent.",
              "Any plan works: Free, Plus or Pro.",
              "Your network stays on this computer; the chat reads it here.",
            ]}
            note="Sign in once; Powerpacks keeps the session. Next: LinkedIn, then a first look at people you know."
          />
          {signedIn ? null : (
            <p className="m-0 text-xs text-muted-foreground">
              No ChatGPT account yet? A free one is enough:{" "}
              <a
                href={SIGN_UP_URL}
                className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
              >
                create one in your browser ↗
              </a>
              , then sign in here.
            </p>
          )}
        </>
      }
    />
  )
}
