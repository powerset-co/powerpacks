import { useRef, useState, type RefObject } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { buildSearchFeedback } from "@/lib/searches/feedback"
import type { FeedbackRecord } from "@/types/searches"

import { FeedbackFooter } from "./FeedbackFooter"
import { NotesField } from "./NotesField"
import { send, type FeedbackSink } from "./send"

export interface SearchFeedbackDialogProps extends FeedbackSink {
  runId: string
  /** The search's title: the dialog's context line and the trigger's label. */
  title: string
}

/** The flag button on a search, which opens a note on what should change. */
export function SearchFeedbackDialog({ runId, title, submit, onToast }: SearchFeedbackDialogProps) {
  const [open, setOpen] = useState(false)
  const notes = useRef<HTMLTextAreaElement>(null)

  function done(record: FeedbackRecord) {
    setOpen(false)
    send({ submit, onToast }, record)
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        aria-label={`Send feedback about ${title}`}
        title="Send feedback"
        className="inline-grid min-h-8 min-w-8 cursor-pointer place-items-center rounded-full border border-border bg-secondary px-2 py-1.5 text-foreground transition-[background-color,transform] duration-fast ease-out hover:bg-line-strong active:scale-95"
      >
        <FlagIcon />
      </DialogTrigger>
      <DialogContent
        className="w-[min(448px,calc(100%-32px))] gap-5 p-6"
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          notes.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">Search feedback</DialogTitle>
          <DialogDescription className="text-[13px]">{title}</DialogDescription>
        </DialogHeader>
        <SearchForm notes={notes} onDone={(comment) => done(buildSearchFeedback(runId, comment))} />
      </DialogContent>
    </Dialog>
  )
}

interface SearchFormProps {
  notes: RefObject<HTMLTextAreaElement>
  onDone: (comment: string) => void
}

// Mounted per opening (the dialog unmounts its content once closed), so each opens empty.
function SearchForm({ notes, onDone }: SearchFormProps) {
  const [comment, setComment] = useState("")
  const ready = comment.trim() !== ""

  function save() {
    if (ready) onDone(comment)
  }

  return (
    <form
      className="grid gap-5"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <NotesField
        ref={notes}
        label="Notes"
        optional={false}
        placeholder="What should change?"
        value={comment}
        onChange={setComment}
        onSave={save}
      />
      <FeedbackFooter action="Send" disabled={!ready} />
    </form>
  )
}

// rendering.py FLAG_SVG.
function FlagIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className="size-[13px]"
    >
      <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
      <line x1="4" x2="4" y1="22" y2="15" />
    </svg>
  )
}
