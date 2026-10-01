import { useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react"

import { errorText } from "@/lib/api/http"
import { openSignIn, postFeedback, ReviewError } from "@/lib/api/review"
import { cn } from "@/lib/utils"

import { useReview } from "../hooks/useReview"
import { FEEDBACK } from "./copy"
import { CheckIcon, SendIcon } from "./icons"
import type { PopoverPlace } from "./place"

const MAX_COMMENT_CHARS = 4000
/** The textarea grows with its text up to this height, in pixels. */
const MAX_FIELD_HEIGHT = 140
/** The textarea takes focus once the popover has popped in. */
const FOCUS_AFTER_MS = 80
/** "Got it, thanks!" stays this long, then the popover closes. */
const THANKS_MS = 900

type Sending = "writing" | "sending" | "sent"

interface FeedbackPopoverProps {
  /** The person menu the popover hangs under; a click on it does not count as outside. */
  anchor: RefObject<HTMLDivElement>
  place: PopoverPlace
  /** "Feedback on <name> — wrong or missing info?" */
  context: string
  /** The candidate's `row_key`. */
  pub: string
  /** The parent's slug. */
  slug: string
  /** Stable across renders: the outside-click listener is tied to it. */
  onClose: () => void
}

// reconcile_review.js `feedbackPopover`: optional feedback on the person. A context line, a
// textarea that grows, ⌘/Ctrl+Enter or the send button, then a "Got it, thanks!" beat before
// it closes. Skip, Escape in the textarea, or a click outside closes it. A refusal for want
// of a Powerset sign-in offers the sign-in; every refusal leaves Send usable again.
export function FeedbackPopover({ anchor, place, context, pub, slug, onClose }: FeedbackPopoverProps) {
  const { toastError } = useReview()
  const popover = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLTextAreaElement>(null)
  const [comment, setComment] = useState("")
  const [sending, setSending] = useState<Sending>("writing")
  const [signInOffered, setSignInOffered] = useState(false)
  const sent = sending === "sent"

  useEffect(() => {
    const timer = window.setTimeout(() => field.current?.focus(), FOCUS_AFTER_MS)
    return () => window.clearTimeout(timer)
  }, [])

  useEffect(() => {
    // The thanks beat closes itself.
    if (sent) return
    const away = (event: MouseEvent) => {
      const target = event.target
      if (target instanceof Node && (popover.current?.contains(target) || target === anchor.current)) return
      onClose()
    }
    // Listening starts once the click that opened the popover has finished bubbling.
    const timer = window.setTimeout(() => document.addEventListener("click", away), 0)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener("click", away)
    }
  }, [sent, anchor, onClose])

  useEffect(() => {
    if (!sent) return
    const timer = window.setTimeout(onClose, THANKS_MS)
    return () => window.clearTimeout(timer)
  }, [sent, onClose])

  const send = async () => {
    const text = comment.trim()
    if (!text || sending !== "writing") return
    setSending("sending")
    try {
      await postFeedback({ pub, parent_slug: slug, comment: text, action: "general" })
    } catch (error) {
      toastError(errorText(error))
      if (error instanceof ReviewError && error.needsAuth) setSignInOffered(true)
      setSending("writing")
      return
    }
    setSending("sent")
  }

  const write = (box: HTMLTextAreaElement) => {
    setComment(box.value)
    box.style.height = "auto"
    box.style.height = `${Math.min(box.scrollHeight, MAX_FIELD_HEIGHT)}px`
  }

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void send()
    }
    if (event.key === "Escape") onClose()
  }

  return (
    <div
      className={cn("feedback-popover", sent && "feedback-done")}
      style={{ top: place.top, right: place.right }}
      ref={popover}
    >
      {sent ? (
        <>
          <span className="feedback-done-badge">
            <CheckIcon />
          </span>
          <p>{FEEDBACK.thanks}</p>
        </>
      ) : (
        <>
          <p className="feedback-context">{context}</p>
          <textarea
            ref={field}
            rows={2}
            maxLength={MAX_COMMENT_CHARS}
            placeholder={FEEDBACK.placeholder}
            value={comment}
            onChange={(event) => write(event.target)}
            onKeyDown={onKeyDown}
          />
          <div className="feedback-footer">
            <span className="feedback-hint">{FEEDBACK.hint}</span>
            <span className="feedback-actions">
              <button
                type="button"
                className="feedback-skip"
                disabled={sending === "sending"}
                onClick={onClose}
              >
                {FEEDBACK.skip}
              </button>
              <button
                type="button"
                className="feedback-send"
                aria-label={FEEDBACK.send}
                disabled={sending === "sending" || !comment.trim()}
                onClick={() => void send()}
              >
                <SendIcon />
              </button>
            </span>
          </div>
          {signInOffered ? <SignIn /> : null}
        </>
      )}
    </div>
  )
}

// reconcile_review.js `signInButton`: one click starts the browser sign-in on this machine.
// It stays "Waiting for sign-in…" once the sign-in has opened; a failure hands it back.
function SignIn() {
  const { toast, toastError } = useReview()
  const [waiting, setWaiting] = useState(false)

  const open = async () => {
    setWaiting(true)
    try {
      await openSignIn()
    } catch (error) {
      toastError(errorText(error))
      setWaiting(false)
      return
    }
    toast(FEEDBACK.signInOpened)
  }

  return (
    <button type="button" className="feedback-login" disabled={waiting} onClick={() => void open()}>
      {waiting ? FEEDBACK.waiting : FEEDBACK.signIn}
    </button>
  )
}
