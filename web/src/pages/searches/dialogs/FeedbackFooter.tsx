import { Button } from "@/components/ui/button"
import { DialogClose, DialogFooter } from "@/components/ui/dialog"

interface FeedbackFooterProps {
  action: string
  disabled: boolean
}

export function FeedbackFooter({ action, disabled }: FeedbackFooterProps) {
  return (
    <DialogFooter className="items-center justify-between">
      <span className="text-[10px] text-muted-foreground">⌘ / Ctrl + Enter to {action.toLowerCase()}</span>
      <span className="inline-flex gap-2">
        <DialogClose asChild>
          <Button type="button">Cancel</Button>
        </DialogClose>
        <Button type="submit" variant="primary" disabled={disabled}>
          {action}
        </Button>
      </span>
    </DialogFooter>
  )
}
