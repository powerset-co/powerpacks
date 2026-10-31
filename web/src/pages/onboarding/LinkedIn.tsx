import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"

import { CheckIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { fetchSignedIn } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { connectSource, continueSetup, fetchConnection } from "@/lib/api/install"
import { live, POLL_MS, prepare, standing } from "@/pages/install/sources"

import { Frame, Heading, Info, Note } from "./Frame"
import { after } from "./steps"

const SIGNED_IN = ["signed-in"]

/** Connect LinkedIn: the one sign-in the first look needs. The sign-in page opens in the Chrome
 *  window Powerpacks drives; once the connections list shows, the window closes and this moves on. */
export function LinkedIn() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  // Continue starts setup; the spinner stays until the Set up screen takes over.
  const [starting, setStarting] = useState(false)
  // The sign-in a click started, until its outcome is known (Connect.tsx keeps the same rule).
  const [queued, setQueued] = useState<{ launched: boolean } | null>(null)
  const connection = useQuery({
    queryKey: ["install", "connect"],
    queryFn: ({ signal }) => fetchConnection(signal),
    refetchInterval: POLL_MS,
  })
  const signed = useQuery({ queryKey: SIGNED_IN, queryFn: ({ signal }) => fetchSignedIn(signal) })
  useEffect(() => {
    const again = () => void client.invalidateQueries({ queryKey: SIGNED_IN })
    window.addEventListener("focus", again)
    return () => window.removeEventListener("focus", again)
  }, [client])
  const record = connection.data
  const running = record !== undefined && live(record)
  const wasRunning = useRef(false)
  useEffect(() => {
    const ended = wasRunning.current && !running
    wasRunning.current = running
    if (ended || (queued?.launched && !running)) {
      void client.refetchQueries({ queryKey: SIGNED_IN }).finally(() => setQueued(null))
    }
  }, [queued, running, client])
  const run = standing(record, signed.data, "linkedin")
  const at = queued && run.state !== "working" ? { state: "working" as const } : run
  const next = after("linkedin")

  const connect = async () => {
    setError(null)
    setQueued({ launched: false })
    try {
      await prepare("linkedin")
      await connectSource("linkedin")
      await client.refetchQueries({ queryKey: ["install", "connect"] })
      setQueued({ launched: true })
    } catch (caught) {
      setError(errorText(caught))
      setQueued(null)
    }
  }
  // Setup starts here, so the next screen opens on its progress, not on another button.
  const start = () => {
    setError(null)
    setStarting(true)
    continueSetup({ sources: ["linkedin"] })
      .then(() => next && void navigate(next.href, { state: { startedAt: Date.now() } }))
      .catch((caught: unknown) => {
        setError(errorText(caught))
        setStarting(false)
      })
  }

  return (
    <Frame
      step="linkedin"
      left={
        <>
          <Heading title="Connect LinkedIn" line="Your connections are where Powerpacks starts." />
          {at.state === "ready" ? (
            <>
              <Note>
                <span className="inline-flex items-center gap-1.5 text-ok">
                  <CheckIcon className="size-4" /> LinkedIn is connected.
                </span>
              </Note>
              <Button variant="primary" size="lg" disabled={starting} onClick={start}>
                {starting ? (
                  <>
                    <Spinner /> Starting setup…
                  </>
                ) : (
                  "Continue"
                )}
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="primary"
                size="lg"
                disabled={at.state === "working" || at.state === "checking"}
                onClick={() => void connect()}
              >
                {at.state === "working" ? (
                  <>
                    <Spinner /> Sign in to LinkedIn in the window that opened
                  </>
                ) : "stopped" in at ? (
                  "Try again"
                ) : (
                  "Connect LinkedIn ↗"
                )}
              </Button>
              {"stopped" in at ? (
                <Note bad>
                  {at.line}
                  {at.detail ? <span className="mt-1 block text-muted-foreground">{at.detail}</span> : null}
                </Note>
              ) : null}
              <Note>
                Opens LinkedIn in a browser window. Come back here once you are in; it closes on its own.
              </Note>
            </>
          )}
          {error ? <Note bad>{error}</Note> : null}
        </>
      }
      right={
        <Info
          title="What Powerpacks reads"
          lines={[
            "Your connections: names, titles, companies",
            "Nothing is posted, liked or sent",
            "If LinkedIn slows down, we stop and ask for your data export instead",
          ]}
          note="Your LinkedIn password never touches Powerpacks."
        />
      }
    />
  )
}
