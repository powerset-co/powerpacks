import { useEffect, useState, type FormEvent, type RefObject } from "react"

import { routeGuidance } from "@/lib/review/guidance"

import { useReview } from "../hooks/useReview"
import { GUIDANCE } from "./copy"

const MAX_GUIDANCE_CHARS = 2000

interface GuidanceFormProps {
  open: boolean
  /** The box was opened or shut by its own summary. */
  onToggle: (open: boolean) => void
  /** The textarea, so "No" / "None of these" can put the caret in it. */
  field: RefObject<HTMLTextAreaElement>
  /** Retarget is off: a request for this card is out, or its re-research is already queued. */
  disabled: boolean
  /** Re-research is queued, and the card is still here to say so. */
  queued: boolean
  /** The text held a LinkedIn profile URL: apply it (free). */
  onFix: (url: string) => void
  /** Any other text: re-research the person from it (paid). */
  onRetarget: (guidance: string) => void
}

// The guidance box: one collapsed box, two routes. A pasted LinkedIn URL applies directly; a
// description of the right person goes to re-research. `routeGuidance` decides.
export function GuidanceForm({
  open,
  onToggle,
  field,
  disabled,
  queued,
  onFix,
  onRetarget,
}: GuidanceFormProps) {
  const { setGuidanceDraft } = useReview()
  const [text, setText] = useState("")
  const typed = Boolean(text.trim())

  // The page never moves the screen under a typed draft.
  useEffect(() => {
    setGuidanceDraft(typed)
    return () => setGuidanceDraft(false)
  }, [typed, setGuidanceDraft])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const route = routeGuidance(text)
    switch (route.kind) {
      case "fix":
        onFix(route.url)
        return
      case "retarget":
        onRetarget(route.guidance)
        return
      case "nothing":
        return
    }
  }

  return (
    <details
      className="retarget-guidance"
      open={open}
      onToggle={(event) => onToggle(event.currentTarget.open)}
    >
      <summary>{GUIDANCE.summary}</summary>
      <form className="retarget-form" onSubmit={submit}>
        <textarea
          ref={field}
          name="guidance"
          maxLength={MAX_GUIDANCE_CHARS}
          required
          placeholder={GUIDANCE.placeholder}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <button className="button button-primary" type="submit" disabled={disabled}>
          {GUIDANCE.submit}
        </button>
        <span hidden={!queued}>{queued ? GUIDANCE.queued : null}</span>
      </form>
    </details>
  )
}
