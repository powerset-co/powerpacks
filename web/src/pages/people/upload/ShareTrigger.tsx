import { Button } from "@/components/ui/button"
import { DialogTrigger } from "@/components/ui/dialog"
import type { UploadStatus } from "@/lib/api/upload"
import { lastUploadLine, UPLOAD } from "@/lib/people/copy"

interface ShareTriggerProps {
  status: UploadStatus | undefined
  busy: boolean
  onOpen: () => void
}

function label(status: UploadStatus | undefined, busy: boolean): string {
  if (busy) return UPLOAD.view
  return status?.last_upload ? UPLOAD.shareChanges : UPLOAD.share
}

/** The last upload in a muted line, then the button that opens the dialog. */
export function ShareTrigger({ status, busy, onOpen }: ShareTriggerProps) {
  return (
    <div className="head-share">
      {status && <span className="head-note">{lastUploadLine(status.last_upload)}</span>}
      <DialogTrigger asChild>
        <Button onClick={onOpen}>
          {busy && <Spinner />}
          {label(status, busy)}
        </Button>
      </DialogTrigger>
    </div>
  )
}

// One turn per --t-slow; the reduced-motion rule in index.css stops it.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="size-3 animate-[turn_var(--t-slow)_linear_infinite] rounded-full border-2 border-current border-r-transparent"
    />
  )
}
