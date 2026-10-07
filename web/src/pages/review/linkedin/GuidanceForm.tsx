import { useState, type FormEvent, type RefObject } from "react"

import { routeGuidance } from "@/lib/review/guidance"

import { GUIDANCE } from "./copy"

const MAX_GUIDANCE_CHARS = 2000

interface GuidanceFormProps {
  open: boolean
  /** The box was opened or shut by its own summary. */
  onToggle: (open: boolean) => void
  /** The textarea, so "No" / "None of these" can put the caret in it. */
  field: RefObject<HTMLTextAreaElement>
  /** Retarget is off: the card takes no decision. */
  disabled: boolean
  /** The text held a LinkedIn profile URL: apply it (free). */
  onFix: (url: string) => void
  /** Any other text: re-research the person from it (paid). */
  onRetarget: (guidance: string) => void
}

// The guidance box: one collapsed box, two routes. A pasted LinkedIn URL applies directly; a
// description of the right person goes to re-research. `routeGuidance` decides.
export function GuidanceForm({ open, onToggle, field, disabled, onFix, onRetarget }: GuidanceFormProps) {
  const [text, setText] = useState("")

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
      </form>
    </details>
  )
}
