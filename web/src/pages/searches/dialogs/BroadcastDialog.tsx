import { useEffect, useState } from "react"

import { HiveIcon, type ToastMessage } from "@/components/shared"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { fetchAskPreview, fetchAskStatus, sendAsk, type AskPreview, type AskStatus } from "@/lib/api/searches"

const DEFAULT_QUESTION = "Would you recommend them, and would you intro?"
const STATUS_POLL_MS = 10_000

export interface BroadcastDialogProps {
  runId: string
  title: string
  /** How many candidates are pinned, from the loaded tags; the preview is the source of truth once open. */
  pinned: number
  onToast: (message: ToastMessage) => void
}

/** The hive button beside the flag: which pinned candidates go out to the set, which
 *  operators receive them and how many each, then the answers as they land. */
export function BroadcastDialog({ runId, title, pinned, onToast }: BroadcastDialogProps) {
  const [open, setOpen] = useState(false)
  const [preview, setPreview] = useState<AskPreview | null>(null)
  const [status, setStatus] = useState<AskStatus | null>(null)
  const [question, setQuestion] = useState(DEFAULT_QUESTION)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let live = true
    const poll = () => {
      fetchAskStatus(runId)
        .then((loaded) => {
          if (live) setStatus(loaded)
        })
        .catch(() => undefined)
    }
    fetchAskPreview(runId)
      .then((loaded) => {
        if (live) setPreview(loaded)
      })
      .catch((failure: unknown) => {
        if (live) setError(failure instanceof Error ? failure.message : String(failure))
      })
    poll()
    const timer = window.setInterval(poll, STATUS_POLL_MS)
    return () => {
      live = false
      window.clearInterval(timer)
    }
  }, [open, runId])

  async function send() {
    setSending(true)
    try {
      const result = await sendAsk(runId, question)
      if (result.status !== "uploaded") throw new Error(`Send failed: ${result.status}`)
      onToast({ message: `Asked ${result.ask?.candidates.length ?? 0} candidates.` })
      setStatus(await fetchAskStatus(runId))
    } catch (failure: unknown) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setSending(false)
    }
  }

  const sent = status?.ask ?? null
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setError(null)
        setOpen(next)
      }}
    >
      <DialogTrigger
        aria-label={`Ask the set about ${title}`}
        title="Ask the set"
        className="inline-grid min-h-8 min-w-8 cursor-pointer place-items-center rounded-full border border-border bg-secondary px-2 py-1.5 text-foreground transition-[background-color,transform] duration-fast ease-out hover:bg-line-strong active:translate-y-px"
      >
        <HiveIcon className="size-[15px]" />
      </DialogTrigger>
      <DialogContent className="w-[min(520px,calc(100%-32px))] gap-5 p-6">
        <DialogHeader>
          <DialogTitle>Ask the set</DialogTitle>
          <DialogDescription>
            {pinned === 0
              ? "Pin candidates first; pinned candidates are what goes out."
              : `${pinned} pinned. Teammates who know them answer from their own laptops.`}
          </DialogDescription>
        </DialogHeader>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        {preview ? (
          <div className="grid gap-4 text-sm">
            <section>
              <h3 className="mb-1 font-medium">Going out</h3>
              {preview.pinned.length === 0 ? (
                <p className="text-muted-foreground">Nothing pinned with a LinkedIn URL.</p>
              ) : (
                <ul className="grid gap-0.5">
                  {preview.candidates.map((candidate) => (
                    <li key={candidate.public_identifier} className="flex justify-between gap-3">
                      <span>{candidate.name}</span>
                      <span className="text-muted-foreground">
                        {candidate.owners.length === 0 ? "nobody in the set knows them" : candidate.owners.map((owner) => owner.name).join(", ")}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {preview.skipped > 0 ? (
                <p className="mt-1 text-muted-foreground">{preview.skipped} pinned without a LinkedIn URL, not sent.</p>
              ) : null}
            </section>
            <section>
              <h3 className="mb-1 font-medium">Sending to</h3>
              {preview.operators.length === 0 ? (
                <p className="text-muted-foreground">No one in the set knows these candidates.</p>
              ) : (
                <ul className="grid gap-0.5">
                  {preview.operators.map((operator) => (
                    <li key={operator.operator_id} className="flex justify-between gap-3">
                      <span>{operator.name}</span>
                      <span className="text-muted-foreground">
                        {operator.candidates} {operator.candidates === 1 ? "contact" : "contacts"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            {sent ? (
              <section>
                <h3 className="mb-1 font-medium">Answers</h3>
                <p className="mb-1 text-muted-foreground">“{sent.question}”</p>
                <ul className="grid gap-0.5">
                  {status?.answers?.candidates.map((candidate) => (
                    <li key={candidate.public_identifier}>
                      <span>{candidate.name}</span>
                      <ul className="ml-4 grid gap-0.5 text-muted-foreground">
                        {candidate.owners.length === 0 ? <li>nobody asked</li> : null}
                        {candidate.owners.map((owner) => (
                          <li key={owner.operator_id}>
                            {owner.name}:{" "}
                            {owner.answer
                              ? `${owner.answer.verdict.replace("_", " ")}${owner.answer.can_intro ? ", can intro" : ""}. ${owner.answer.reason}`
                              : owner.status === "declined"
                                ? "not in their store"
                                : owner.awake
                                  ? "pending"
                                  : "offline, pending"}
                          </li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              </section>
            ) : (
              <section className="grid gap-2">
                <label className="grid gap-1">
                  <span className="font-medium">Question</span>
                  <textarea
                    value={question}
                    onChange={(event) => setQuestion(event.target.value)}
                    rows={2}
                    className="rounded-md border border-border bg-background px-2 py-1.5"
                  />
                </label>
                <button
                  type="button"
                  disabled={sending || preview.operators.length === 0}
                  onClick={() => void send()}
                  className="justify-self-end rounded-md border border-border bg-secondary px-3 py-1.5 font-medium hover:bg-line-strong disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {sending ? "Sending…" : "Send"}
                </button>
              </section>
            )}
          </div>
        ) : error ? null : (
          <p className="text-sm text-muted-foreground">Loading who would be asked…</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
