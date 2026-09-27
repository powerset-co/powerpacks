import { Button } from "@/components/ui/button"
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { UPLOAD, UPLOAD_SENTENCE, UPLOAD_TITLE } from "@/lib/people/copy"
import { resumes, type UploadView } from "@/lib/people/upload"
import { cn } from "@/lib/utils"

import { UploadDone, UploadProgress } from "./UploadProgress"
import { UploadSummary } from "./UploadSummary"
import type { Upload } from "./useUpload"

function title({ view, status }: Upload): string {
  if (view.phase === "completed" && status?.last_upload?.uploaded === 0) return UPLOAD.titleUpToDate
  return UPLOAD_TITLE[view.phase]
}

function sentence(view: UploadView): string | null {
  return "error" in view ? view.error : UPLOAD_SENTENCE[view.phase]
}

/** One column: title, one sentence, the plan, the live progress or the result, the buttons. */
export function UploadDialog({ upload }: { upload: Upload }) {
  const { view, status } = upload
  const running = view.phase === "checking" || view.phase === "uploading"
  const plan = view.phase === "ready" || view.phase === "uploading" ? status?.plan : null
  const last = view.phase === "completed" ? status?.last_upload : null
  return (
    <DialogContent className="gap-5 p-6" onKeyDown={(event) => event.stopPropagation()}>
      <DialogHeader>
        <DialogTitle className="text-lg font-semibold">{title(upload)}</DialogTitle>
        <DialogDescription className={cn("text-[13px]", "error" in view && "text-bad")}>
          {sentence(view)}
        </DialogDescription>
      </DialogHeader>
      {plan && <UploadSummary plan={plan} />}
      {running && <UploadProgress status={status} reconnecting={upload.reconnecting} />}
      {last && <UploadDone last={last} />}
      <DialogFooter>
        <DialogClose asChild>
          <Button variant="ghost">{UPLOAD.close}</Button>
        </DialogClose>
        <Actions upload={upload} />
      </DialogFooter>
    </DialogContent>
  )
}

function Actions({ upload }: { upload: Upload }) {
  switch (upload.view.phase) {
    case "checking":
    case "uploading":
      return null
    case "ready":
      return (
        <>
          <Button variant="ghost" onClick={upload.check}>
            {UPLOAD.checkAgain}
          </Button>
          <Button variant="primary" onClick={upload.confirm}>
            {resumes(upload.status) ? UPLOAD.resume : UPLOAD.confirm}
          </Button>
        </>
      )
    case "idle":
      return (
        <Button variant="primary" onClick={upload.check}>
          {UPLOAD.check}
        </Button>
      )
    case "completed":
      return <Button onClick={upload.check}>{UPLOAD.checkAgain}</Button>
    case "check-failed":
    case "upload-failed":
    case "interrupted":
    case "refused":
      return (
        <Button variant="primary" onClick={upload.check}>
          {UPLOAD.checkAgain}
        </Button>
      )
  }
}
