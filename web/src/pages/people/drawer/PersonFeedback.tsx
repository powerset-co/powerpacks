import { useRef, useState } from "react"

import { FeedbackForm, FlagIcon } from "@/components/shared"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { sendPersonFeedback } from "@/lib/api/feedback"
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
        <FeedbackForm
          notes={notes}
          placeholder="What should change?"
          send={(comment) => sendPersonFeedback(comment, row)}
        />
      </DialogContent>
    </Dialog>
  )
}
