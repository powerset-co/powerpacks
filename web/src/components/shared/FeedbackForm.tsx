import { useState, type RefObject } from "react"

import { Button } from "@/components/ui/button"
import { DialogClose, DialogFooter } from "@/components/ui/dialog"
import { errorText } from "@/lib/api/http"

import { FeedbackFooter } from "./FeedbackFooter"
import { NotesField } from "./NotesField"

interface FeedbackFormProps {
  notes: RefObject<HTMLTextAreaElement>
  placeholder: string
  /** Files the note; throws why it did not go out. */
  send: (comment: string) => Promise<void>
}

/** A note for the Powerset team inside a dialog: Notes and Send, then "Sent. Thank you." Mounted per
 *  opening (the dialog unmounts its content once closed), so each opens empty. */
export function FeedbackForm({ notes, placeholder, send: file }: FeedbackFormProps) {
  const [comment, setComment] = useState("")
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const ready = comment.trim() !== "" && !sending

  async function send() {
    if (!ready) return
    setSending(true)
    setProblem(null)
    try {
      await file(comment)
      setSent(true)
    } catch (error) {
      setProblem(errorText(error))
    } finally {
      setSending(false)
    }
  }

  if (sent) {
    return (
      <>
        <p role="status" className="m-0 text-[13px] text-ok">
          Sent. Thank you.
        </p>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="primary">Done</Button>
          </DialogClose>
        </DialogFooter>
      </>
    )
  }
  return (
    <form
      className="grid gap-5"
      onSubmit={(event) => {
        event.preventDefault()
        void send()
      }}
    >
      <NotesField
        ref={notes}
        label="Notes"
        optional={false}
        placeholder={placeholder}
        value={comment}
        onChange={setComment}
        onSave={() => void send()}
      />
      {problem && (
        <p role="alert" className="m-0 text-[13px] text-bad">
          {problem}
        </p>
      )}
      <FeedbackFooter action="Send" disabled={!ready} />
    </form>
  )
}
