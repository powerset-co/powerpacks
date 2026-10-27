import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"

import { Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { ACCOUNT_KEY, fetchAccount } from "@/lib/api/account"
import { signIn } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"

import { Frame, Heading, Info, Note } from "./Frame"
import { after } from "./steps"

// While the sign-in is out in the browser, ask who is signed in this often.
const POLL_MS = 2_000

/** Sign up: the Powerset sign-in in the browser, the account the invite email named. Signed in,
 *  the screen moves on by itself. */
export function SignUp() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [signingIn, setSigningIn] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const account = useQuery({
    queryKey: ACCOUNT_KEY,
    queryFn: ({ signal }) => fetchAccount(signal),
    refetchInterval: signingIn ? POLL_MS : false,
  })
  const signedIn = account.data?.signed_in === true
  const next = after("signup")
  useEffect(() => {
    if (signingIn && signedIn && next) void navigate(next.href)
  }, [signingIn, signedIn, next, navigate])

  const start = () => {
    setError(null)
    setSigningIn(true)
    void signIn()
      .catch((caught: unknown) => {
        setError(errorText(caught))
        setSigningIn(false)
      })
      .finally(() => void client.invalidateQueries({ queryKey: ACCOUNT_KEY }))
  }

  return (
    <Frame
      step="signup"
      left={
        <>
          <span className="text-[11px] font-semibold tracking-[.2em] text-muted-foreground">
            POWER<span className="text-primary">SET</span>
          </span>
          <Heading title="Let’s get you started" line="Your network, searchable." />
          {signedIn ? (
            <>
              <Note>Signed in as {account.data?.email ?? "your account"}.</Note>
              <Button variant="primary" size="lg" onClick={() => next && void navigate(next.href)}>
                Continue
              </Button>
            </>
          ) : (
            <>
              <Button variant="primary" size="lg" disabled={signingIn || account.isPending} onClick={start}>
                {signingIn ? (
                  <>
                    <Spinner /> Finish signing in in your browser
                  </>
                ) : (
                  "Sign in via browser ↗"
                )}
              </Button>
              <Note>Use the Powerset account from your invite email.</Note>
            </>
          )}
          {error ? <Note bad>{error}</Note> : null}
          <InviteCode />
        </>
      }
      right={
        <Info
          title="What happens next"
          lines={[
            "Sign in to ChatGPT, which the chat runs on",
            "Connect LinkedIn",
            "A first look at people you know, in minutes",
            "Ask anything about your network",
          ]}
          note="Messages, Gmail and WhatsApp can be added later."
        />
      }
    />
  )
}

const LINK =
  "cursor-pointer self-start border-0 bg-transparent p-0 text-xs text-muted-foreground underline hover:text-foreground"

/** The invite code, for someone without an account yet: a small dialog to type it into. The
 *  Powerset API does not take codes yet, so the dialog says so and the button stays off. */
function InviteCode() {
  const [open, setOpen] = useState(false)
  const [code, setCode] = useState("")
  return (
    <>
      <button type="button" className={LINK} onClick={() => setOpen(true)}>
        Have an invite code?
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Enter your invite code</DialogTitle>
          <DialogDescription>
            The code from your invite message, with the account it is for.
          </DialogDescription>
          <DialogBody>
            <Input
              value={code}
              className="focus-visible:ring-0 focus-visible:ring-offset-0"
              placeholder="e.g. POWER-7F3K"
              aria-label="Invite code"
              onChange={(event) => setCode(event.target.value.toUpperCase())}
            />
          </DialogBody>
          <DialogFooter>
            <Note>Codes are not live yet; the account sign-in is the way in for now.</Note>
            <Button variant="primary" disabled>
              Use code
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
