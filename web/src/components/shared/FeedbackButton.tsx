import { useRef, useState, type RefObject } from "react"
import { useLocation } from "react-router-dom"

import { sendAppFeedback } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"

import { Button } from "../ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "../ui/dialog"
import { FeedbackFooter } from "./FeedbackFooter"
import { FlagIcon } from "./icons/actions"
import { NotesField } from "./NotesField"
import { Tip } from "./Tip"

const TITLE = "Send feedback"

/** The side nav's flag beside who is signed in: a note on the app for the Powerset team, sent with
 *  the page it was written on. */
export function FeedbackButton({ rail }: { rail: boolean }) {
  const [open, setOpen] = useState(false)
  const notes = useRef<HTMLTextAreaElement>(null)
  const page = useLocation().pathname
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        aria-label={TITLE}
        className="group relative grid size-8 shrink-0 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
      >
        <FlagIcon className="size-4" />
        <Tip side={rail ? "right" : "above"}>{TITLE}</Tip>
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
          <DialogTitle className="text-lg font-semibold">{TITLE}</DialogTitle>
          <DialogDescription className="text-[13px]">To the Powerset team</DialogDescription>
        </DialogHeader>
        <FeedbackForm notes={notes} page={page} />
      </DialogContent>
    </Dialog>
  )
}

interface FeedbackFormProps {
  notes: RefObject<HTMLTextAreaElement>
  page: string
}

// Mounted per opening (the dialog unmounts its content once closed), so each opens empty.
function FeedbackForm({ notes, page }: FeedbackFormProps) {
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
      await sendAppFeedback(comment, page)
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
