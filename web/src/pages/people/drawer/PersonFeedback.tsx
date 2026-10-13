import { useRef, useState, type RefObject } from "react"

import { FeedbackFooter, FlagIcon, NotesField } from "@/components/shared"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { sendPersonFeedback } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import type { Person } from "@/types/people"

/** The drawer's flag beside Close: a note about this person for the Powerset team, sent with who
 *  they are and the worth and share call shown (as a search's drawer flags a candidate). */
export function PersonFeedback({ row }: { row: Person }) {
  const [open, setOpen] = useState(false)
  const notes = useRef<HTMLTextAreaElement>(null)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        className="drawer-icon"
        aria-label={`Send feedback about ${row.name}`}
        title="Send feedback"
      >
        <FlagIcon className="size-[15px]" />
      </DialogTrigger>
      <DialogContent
        className="w-[min(448px,calc(100%-32px))] gap-5 p-6"
        // Typing a note must not reach the page's own keys.
        onKeyDown={(event) => event.stopPropagation()}
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          notes.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">Person feedback</DialogTitle>
          <DialogDescription className="text-[13px]">{row.name}</DialogDescription>
        </DialogHeader>
        <FeedbackForm notes={notes} row={row} />
      </DialogContent>
    </Dialog>
  )
}

interface FeedbackFormProps {
  notes: RefObject<HTMLTextAreaElement>
  row: Person
}

// Mounted per opening (the dialog unmounts its content once closed), so each opens empty.
function FeedbackForm({ notes, row }: FeedbackFormProps) {
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
      await sendPersonFeedback(comment, row)
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
        placeholder="What should change?"
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
