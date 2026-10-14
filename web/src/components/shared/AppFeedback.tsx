import { useRef } from "react"
import { useLocation } from "react-router-dom"

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { sendAppFeedback } from "@/lib/api/feedback"

import { FeedbackForm } from "./FeedbackForm"

/** The account menu's Send feedback: a note about the app for the Powerset team, with the page it
 *  was sent from. */
export function AppFeedback({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const notes = useRef<HTMLTextAreaElement>(null)
  const { pathname } = useLocation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
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
          <DialogTitle className="text-lg font-semibold">Send feedback</DialogTitle>
          <DialogDescription className="text-[13px]">It goes to the Powerset team.</DialogDescription>
        </DialogHeader>
        <FeedbackForm
          notes={notes}
          placeholder="What's working, what isn't, what you'd want?"
          send={(comment) => sendAppFeedback(comment, pathname)}
        />
      </DialogContent>
    </Dialog>
  )
}
