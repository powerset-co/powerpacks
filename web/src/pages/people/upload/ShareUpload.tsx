import { Dialog } from "@/components/ui/dialog"

import { ShareMenu } from "../sets/ShareMenu"
import { UploadDialog } from "./UploadDialog"
import { useUpload } from "./useUpload"

/** The People head's share button and its dialog; a run that finishes while closed toasts. */
export function ShareUpload({ onToast }: { onToast: (message: string) => void }) {
  const upload = useUpload(onToast)
  return (
    <Dialog open={upload.open} onOpenChange={upload.setOpen}>
      <ShareMenu status={upload.status} busy={upload.busy} onOpen={upload.onOpen} />
      <UploadDialog upload={upload} />
    </Dialog>
  )
}
