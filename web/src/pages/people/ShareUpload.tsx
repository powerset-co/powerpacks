import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { errorText } from "@/lib/api/http"
import { uploadStatus, type UploadStatus } from "@/lib/api/upload"

const UPLOAD_KEY = ["people-upload"]
const UPLOAD_STEPS = 6 // Five document namespaces, then the database commit.

export function ShareUpload() {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState("")
  const posting = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const query = useQuery({
    queryKey: UPLOAD_KEY,
    queryFn: () => uploadStatus(),
    enabled: open && !starting && !startError,
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1000 : false),
    retry: 3,
    staleTime: Infinity,
  })
  const status = query.data
  const running = starting || status?.status === "running"

  async function start() {
    setOpen(true)
    if (posting.current || status?.status === "running") return
    posting.current = true
    setStarting(true)
    setStartError("")
    try {
      client.setQueryData(UPLOAD_KEY, await uploadStatus(true))
    } catch (error) {
      setStartError(errorText(error))
    } finally {
      posting.current = false
      setStarting(false)
    }
  }

  const error =
    startError || (query.error ? "Connection interrupted. Reconnecting to your upload…" : status?.error)
  const failed = Boolean(startError) || status?.status === "failed" || status?.status === "interrupted"
  const completed = !starting && status?.status === "completed"
  const unchanged = completed && status.progress.uploaded === 0 && status.progress.skipped > 0
  const title = failed
    ? "Upload needs attention"
    : completed
      ? unchanged
        ? "Your network is up to date"
        : "Your network is shared"
      : "Uploading your network"

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button ref={trigger} className="mb-2 ml-2 min-h-9" variant="primary" onClick={() => void start()}>
        {running ? "View upload" : "Share network"}
      </Button>
      <DialogContent
        className="gap-6 rounded-[14px] p-6"
        onKeyDown={(event) => event.stopPropagation()}
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          trigger.current?.focus()
        }}
      >
        <DialogHeader>
          <p className="m-0 mb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            Powerset network
          </p>
          <DialogTitle className="text-lg font-semibold">{title}</DialogTitle>
          <DialogDescription className="text-[13px]">
            People marked Share are uploaded. Unchanged people are skipped.
          </DialogDescription>
        </DialogHeader>
        <UploadProgress status={starting ? undefined : status} completed={completed} />
        <div className="min-h-10 text-[13px] leading-relaxed" aria-live="polite">
          {error ? (
            <p role="alert" className="m-0 text-bad">
              {error}
            </p>
          ) : (
            <p className="m-0 text-muted-foreground">
              {starting
                ? "Connecting to your network…"
                : (status?.message ??
                  (completed
                    ? "Sharing decisions are saved. You can check for changes any time."
                    : "You can close this window. Your upload will continue."))}
            </p>
          )}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">{completed ? "Done" : "Close window"}</Button>
          </DialogClose>
          {failed && (
            <Button variant="primary" onClick={() => void start()}>
              Retry upload
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function UploadProgress({ status, completed }: { status: UploadStatus | undefined; completed: boolean }) {
  const { total = 0, uploaded = 0, skipped = 0 } = status?.progress ?? {}
  const processed = uploaded + skipped
  const written = Object.values(status?.progress.namespaces ?? {})
  const records = written.reduce((sum, row) => sum + row.upserted + row.patched, 0)
  const percent = completed ? 100 : Math.round((written.length / UPLOAD_STEPS) * 100)
  return (
    <div className="grid gap-4">
      <div className="flex items-baseline justify-between gap-3 text-[13px] tabular-nums">
        <span className="font-medium">
          {completed
            ? `${processed.toLocaleString()} of ${total.toLocaleString()} people`
            : written.length
              ? `${records.toLocaleString()} records written`
              : "Preparing your upload"}
        </span>
        <span className="text-muted-foreground">{total || completed ? `${percent}%` : ""}</span>
      </div>
      <div
        role="progressbar"
        aria-label="Network upload"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={written.length || completed ? percent : undefined}
        className="h-1.5 overflow-hidden rounded-full bg-secondary"
      >
        <div
          className="h-full origin-left rounded-full bg-ok transition-transform duration-300 motion-reduce:transition-none"
          style={{ transform: `scaleX(${percent / 100})` }}
        />
      </div>
      <dl className="m-0 grid grid-cols-2 gap-4 border-b border-border pb-5">
        <div>
          <dt className="text-xs text-muted-foreground">Uploaded</dt>
          <dd className="m-0 mt-1 text-2xl font-semibold tabular-nums">{uploaded.toLocaleString()}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Already up to date</dt>
          <dd className="m-0 mt-1 text-2xl font-semibold tabular-nums">{skipped.toLocaleString()}</dd>
        </div>
      </dl>
      {Boolean(status?.skipped_no_linkedin) && (
        <p className="m-0 text-xs text-muted-foreground">
          {status?.skipped_no_linkedin} people need a LinkedIn profile before they can be uploaded.
        </p>
      )}
    </div>
  )
}
