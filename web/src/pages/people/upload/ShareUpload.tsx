import { Dialog } from "@/components/ui/dialog"

import { ShareButton } from "./ShareButton"
import { UploadDialog } from "./UploadDialog"
import { useUpload } from "./useUpload"

/** The People head's share button and its dialog; a run that finishes while closed toasts. */
export function ShareUpload({ onToast }: { onToast: (message: string) => void }) {
  const upload = useUpload(onToast)
  return (
    <Dialog open={upload.open} onOpenChange={upload.setOpen}>
      <ShareButton status={upload.status} busy={upload.busy} />
      <UploadDialog upload={upload} />
    </Dialog>
  )
}
