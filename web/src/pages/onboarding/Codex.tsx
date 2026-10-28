import { useEffect, useRef } from "react"
import { useNavigate } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { isDesktop } from "@/lib/desktop"
import { CodexDetails } from "@/pages/settings/CodexDetails"

import { Frame, Heading, Info, Note } from "./Frame"
import { after } from "./steps"

// Where someone without a ChatGPT account makes one. Opens in the system browser; the
// referral code rides on this URL once there is one.
const SIGN_UP_URL = "https://chatgpt.com/auth/login"

/** Codex: the ChatGPT sign-in the chat runs on, the Accounts page's Codex card (who, the plan,
 *  Connected, Sign out, Connect ChatGPT). A sign-in finished here moves the screen on by itself;
 *  someone already signed in sees the card and Continue. */
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
          <Heading title="Sign in to ChatGPT" />
          {isDesktop() ? (
            <>
              <div className="rounded-[var(--radius-m)] border border-line-strong bg-card px-4 py-3.5">
                <CodexDetails codex={codex} explained={false} />
              </div>
              {signedIn ? (
                <Button variant="primary" size="lg" onClick={() => next && void navigate(next.href)}>
                  Continue
                </Button>
              ) : null}
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
