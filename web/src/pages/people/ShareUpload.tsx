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
    enabled: !starting && !startError,
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1000 : false),
    retry: 3,
    staleTime: Infinity,
  })
  const status = query.data
  const running = starting || status?.status === "running"
  const checking =
    starting ||
    (running && (!status?.stage || status.stage.startsWith("checking") || status.stage === "planning"))

  async function start() {
    setOpen(true)
    if (posting.current || status?.status === "running") return
    posting.current = true
    setStarting(true)
    setStartError("")
    try {
      await client.cancelQueries({ queryKey: UPLOAD_KEY })
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
  const failed =
    !starting && (Boolean(startError) || status?.status === "failed" || status?.status === "interrupted")
  const completed = !starting && status?.status === "completed"
  const unchanged = completed && status.progress.uploaded === 0 && status.progress.skipped > 0
  const title = failed
    ? "Upload failed"
    : completed
      ? unchanged
        ? "Your network is up to date"
        : "Your network is shared"
      : checking
        ? "Checking your network"
        : "Uploading your network"

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <div className="mb-2 ml-3 flex flex-wrap items-center justify-end gap-3">
        {completed && (
          <span className="text-xs text-muted-foreground">
            Last upload: {status.progress.total.toLocaleString()} people
          </span>
        )}
        <Button
          ref={trigger}
          className="min-h-9"
          variant={completed ? "default" : "primary"}
          onClick={() => void start()}
        >
          {running && (
            <span
              aria-hidden="true"
              className="size-3 animate-spin rounded-full border-2 border-current border-r-transparent motion-reduce:animate-none"
            />
          )}
          {running ? "View upload" : completed ? "Check for updates" : "Share network"}
        </Button>
      </div>
      <DialogContent
        className="gap-6 rounded-[14px] p-6"
        onKeyDown={(event) => event.stopPropagation()}
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          trigger.current?.focus()
        }}
      >
        <DialogHeader>
          <div className="mb-2 flex items-center gap-3">
            <div
              className={`grid size-11 place-items-center rounded-full border ${failed ? "border-bad-soft bg-bad-soft text-bad" : completed ? "border-ok-soft bg-ok-soft text-ok" : "border-primary-soft bg-primary-soft text-primary"}`}
            >
              {running ? (
                <span
                  role="status"
                  aria-label="Upload in progress"
                  className="size-5 animate-spin rounded-full border-2 border-current border-r-transparent motion-reduce:animate-none"
                />
              ) : (
                <span aria-hidden="true" className="text-xl">
                  {completed ? "✓" : "!"}
                </span>
              )}
            </div>
            <span className="text-xs font-medium text-muted-foreground">
              {failed ? "Failed" : completed ? "Complete" : "In progress"}
            </span>
          </div>
          <DialogTitle className="text-lg font-semibold">{title}</DialogTitle>
          <DialogDescription className="text-[13px]">
            {running ? "Continues in the background." : "Only people marked Share are uploaded."}
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-3 gap-2 text-xs">
          {["Check network", "Upload changes", "Finish"].map((label, index) => {
            const step = checking ? 0 : status?.stage === "committing" || completed ? 2 : 1
            return (
              <div
                key={label}
                className={`border-t-2 pt-2 ${completed || index < step ? "border-ok text-ok" : index === step ? "border-primary text-foreground" : "border-border text-muted-foreground"}`}
              >
                {label}
              </div>
            )
          })}
        </div>
        <UploadProgress
          status={starting ? undefined : status}
          completed={completed}
          running={running}
          checking={checking}
        />
        <div className="min-h-10 text-[13px] leading-relaxed" aria-live="polite">
          {error && !starting ? (
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
                    : "Checking your shared people and saved uploads…"))}
            </p>
          )}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Close</Button>
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

function UploadProgress({
  status,
  completed,
  running,
  checking,
}: {
  status: UploadStatus | undefined
  completed: boolean
  running: boolean
  checking: boolean
}) {
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
        <span className="text-muted-foreground">
          {written.length || completed
            ? `${percent}%`
            : running
              ? checking
                ? "Checking…"
                : "Uploading…"
              : ""}
        </span>
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
          className={`h-full origin-left rounded-full transition-transform duration-300 motion-reduce:transition-none ${running && !written.length ? "w-full animate-pulse bg-primary-soft motion-reduce:animate-none" : "bg-ok"}`}
          style={{ transform: `scaleX(${running && !written.length ? 1 : percent / 100})` }}
        />
      </div>
      <dl className="m-0 grid grid-cols-2 gap-4 border-b border-border pb-5">
        <div>
          <dt className="text-xs text-muted-foreground">{completed ? "Uploaded" : "People to share"}</dt>
          <dd className="m-0 mt-1 text-2xl font-semibold tabular-nums">
            {completed ? uploaded.toLocaleString() : total ? total.toLocaleString() : "—"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">
            {completed ? "Already up to date" : "Records written"}
          </dt>
          <dd className="m-0 mt-1 text-2xl font-semibold tabular-nums">
            {completed ? skipped.toLocaleString() : records.toLocaleString()}
          </dd>
        </div>
      </dl>
      {(Boolean(status?.skipped_no_linkedin) || Boolean(status?.companies_skipped_no_row)) && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Skipped items</summary>
          <div className="mt-2 grid gap-1">
            {Boolean(status?.skipped_no_linkedin) && (
              <p className="m-0">{status?.skipped_no_linkedin} people without LinkedIn</p>
            )}
            {Boolean(status?.companies_skipped_no_row) && (
              <p className="m-0">{status?.companies_skipped_no_row} missing company record</p>
            )}
          </div>
        </details>
      )}
    </div>
  )
}
