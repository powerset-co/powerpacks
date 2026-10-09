import { useEffect, useRef, useState } from "react"

import { errorText } from "@/lib/api/http"
import {
  closeSignIn,
  placeSignIn,
  showSignIn,
  signInHost,
  useReading,
  useSignIn,
  type Bounds,
  type SignIn,
} from "@/lib/signin"

import { CLOSE_MARK } from "./icons/actions"
import { PowersetMark } from "./PowersetMark"
import { Spinner } from "./Spinner"

function LockIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
      className="size-3"
    >
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  )
}

function bounds(element: HTMLElement): Bounds {
  const rect = element.getBoundingClientRect()
  return { x: rect.left, y: rect.top, width: rect.width, height: rect.height }
}

/** The desktop app's sign-in modal: the app dims, and a centered card shows the provider's own
 *  sign-in page in a native view that fills the card's body. */
export function SignInModal() {
  const signIn = useSignIn()
  if (signIn === null) return null
  return <Card key={signIn.url} signIn={signIn} />
}

function Card({ signIn }: { signIn: SignIn }) {
  const body = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)

  // Show the page once the body has a size, then keep the view on it as the window resizes.
  useEffect(() => {
    const element = body.current
    if (!element) return
    showSignIn(signIn, bounds(element)).catch((caught: unknown) => setError(errorText(caught)))
    const observer = new ResizeObserver(() => void placeSignIn(bounds(element)))
    observer.observe(element)
    const onResize = () => void placeSignIn(bounds(element))
    window.addEventListener("resize", onResize)
    return () => {
      observer.disconnect()
      window.removeEventListener("resize", onResize)
    }
  }, [signIn])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") void closeSignIn()
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])

  const host = signInHost(signIn.url)
  const reading = useReading()
  const title = reading ? "Reading your connections" : `Sign in to ${signIn.title}`
  const subtitle = reading
    ? reading.total
      ? `${reading.read.toLocaleString()} of ${reading.total.toLocaleString()} so far. Leave this open.`
      : "Opening your connections list."
    : "Powerpacks picks up as soon as you finish."
  return (
    <div
      role="dialog"
      aria-modal
      aria-label={title}
      className="fade-in fixed inset-0 z-[70] grid place-items-center bg-black/55 p-4 backdrop-blur-[6px]"
    >
      <div className="rise-in flex h-[min(700px,100%)] w-[min(480px,100%)] flex-col overflow-hidden rounded-[var(--radius-l)] border border-line-strong bg-card shadow-[var(--shadow-2)]">
        <header className="flex h-14 items-center gap-3 border-b border-line px-4">
          <PowersetMark className="size-7 rounded-[7px]" />
          <div className="min-w-0 flex-1">
            <h2 className="m-0 truncate text-[14px] font-semibold leading-tight">{title}</h2>
            <p className="m-0 truncate text-[11.5px] leading-tight text-muted-foreground">{subtitle}</p>
          </div>
          <button
            type="button"
            aria-label="Cancel sign-in"
            onClick={() => void closeSignIn()}
            className="grid size-8 cursor-pointer place-items-center rounded-full border-0 bg-transparent text-xl leading-none text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
          >
            {CLOSE_MARK}
          </button>
        </header>
        {/* The native view covers this box once the page paints; until then it shows loading. */}
        <div ref={body} className="relative min-h-0 flex-1 bg-white">
          <div className="absolute inset-0 grid place-items-center">
            {error ? (
              <p className="m-0 max-w-[320px] px-4 text-center text-xs text-bad">{error}</p>
            ) : (
              <p className="m-0 flex items-center gap-2 text-xs text-neutral-500">
                <Spinner />
                Loading {host}
              </p>
            )}
          </div>
        </div>
        <footer className="flex h-9 items-center gap-2 border-t border-line px-4 text-[11px] text-faint">
          <LockIcon />
          <span className="truncate">
            You are signing in directly with <span className="text-muted-foreground">{host}</span>.
          </span>
        </footer>
      </div>
    </div>
  )
}
