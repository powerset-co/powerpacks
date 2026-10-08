import { COPY, copyFailed, TOAST } from "@/lib/review/copy"

import { useReview } from "../hooks/useReview"

interface HandoffCopyProps {
  /** What the user pastes into Codex. */
  phrase: string
}

// The copy-phrase box: the phrase and a Copy button. "Copied" on success; when the clipboard
// refuses, an error naming the phrase to type.
export function HandoffCopy({ phrase }: HandoffCopyProps) {
  const { toast, toastError } = useReview()
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(phrase)
      toast(TOAST.copied)
    } catch {
      toastError(copyFailed(phrase))
    }
  }
  return (
    <div className="handoff-copy">
      <code>{phrase}</code>
      <button className="button button-outline" type="button" onClick={() => void copy()}>
        {COPY}
      </button>
    </div>
  )
}
