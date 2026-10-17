import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { ProgressBar, RefreshIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { errorText } from "@/lib/api/http"
import {
  checkSignals,
  readSignals,
  refreshSignals,
  type SignalsPlan,
  type SignalsStatus,
} from "@/lib/api/signals"
import { countOf, plural } from "@/lib/copy"

import { PEOPLE_QUERY_KEY } from "../hooks/usePeopleQuery"

const SIGNALS_KEY = ["people-signals"]
const TITLE = "Refresh signals"

// The plan's estimate in words: "under a minute", "about 4 minutes".
function duration(seconds: number): string {
  if (seconds < 60) return "under a minute"
  const minutes = Math.round(seconds / 60)
  return minutes === 1 ? "about a minute" : `about ${minutes} minutes`
}

interface RefreshSignalsProps {
  // Empty: whoever needs it (new people, or signals from older questions), and the header button
  // reads All. Else these people too, asked again even when their signals are current, and the
  // button reads how many are selected.
  people: readonly string[]
  // The drawer's round icon beside Close, instead of the header's button.
  icon?: boolean
  onDone: () => void
}

/** Refresh signals from saved facts: says how many people and about how long, asks before any paid
 *  request, then follows the run with a progress bar. */
export function RefreshSignals({ people, icon = false, onDone }: RefreshSignalsProps) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [checking, setChecking] = useState(false)
  const [plan, setPlan] = useState<SignalsPlan | null>(null)
  const [targets, setTargets] = useState<readonly string[]>([])
  const [error, setError] = useState<string | null>(null)
  const query = useQuery({
    queryKey: SIGNALS_KEY,
    queryFn: async () => {
      const before = client.getQueryData<SignalsStatus>(SIGNALS_KEY)
      const status = await readSignals()
      if (before?.status === "running" && status.status === "completed") {
        void client.invalidateQueries({ queryKey: PEOPLE_QUERY_KEY })
        onDone()
      }
      return status
    },
    refetchInterval: (latest) => (latest.state.data?.status === "running" ? 1000 : false),
    staleTime: Infinity,
    retry: false,
  })
  const status = query.data
  const running = status?.status === "running"
  const start = async (ids: readonly string[], checked?: string) => {
    const next = await refreshSignals(ids, checked)
    client.setQueryData(SIGNALS_KEY, next)
    setPlan(null)
    if (next.status === "completed") {
      void client.invalidateQueries({ queryKey: PEOPLE_QUERY_KEY })
      onDone()
    }
  }
  const check = async () => {
    setOpen(true)
    setChecking(true)
    setPlan(null)
    setError(null)
    client.setQueryData<SignalsStatus>(SIGNALS_KEY, {
      status: "idle",
      message: null,
      plan: null,
      error: null,
      done: 0,
      total: 0,
      refused: 0,
    })
    const ids = [...people]
    setTargets(ids)
    try {
      await client.cancelQueries({ queryKey: SIGNALS_KEY })
      const next = await checkSignals(ids)
      // Nothing to pay for starts at once; nobody to refresh starts nothing.
      if (next.new_requests === 0 && next.people > 0) await start(ids)
      else setPlan(next)
    } catch (error) {
      setError(errorText(error))
    } finally {
      setChecking(false)
    }
  }
  const confirm = async () => {
    if (!plan) return
    setChecking(true)
    setError(null)
    try {
      await start(targets, plan.checked)
    } catch (error) {
      setPlan(null)
      setError(errorText(error))
    } finally {
      setChecking(false)
    }
  }
  const failure = error ?? query.error?.message ?? (status?.status === "failed" ? status.error : null)
  const shown = plan ?? status?.plan ?? null
  const openOrCheck = () => (running ? setOpen(true) : void check())
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {icon ? (
          <Button
            shape="pill"
            size="sm"
            title="Re-reads this person's saved facts. Asks before anything costs money."
            disabled={checking}
            onClick={openOrCheck}
          >
            {running ? <Spinner /> : <RefreshIcon className="size-[14px]" />}
            {TITLE}
          </Button>
        ) : (
          <Button disabled={checking} onClick={openOrCheck}>
            {running ? "Refreshing signals…" : TITLE}{" "}
            {running ? (
              <Spinner />
            ) : (
              <span className="font-normal text-muted-foreground">
                {people.length ? `${people.length.toLocaleString()} selected` : "All"}
              </span>
            )}
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="gap-5 p-6" onKeyDown={(event) => event.stopPropagation()}>
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">{TITLE}</DialogTitle>
          <DialogDescription className="text-[13px]">
            {shown
              ? `${plural(shown.people, "person")} · ${duration(shown.estimated_seconds)}`
              : "Checking saved signals…"}
          </DialogDescription>
        </DialogHeader>
        <div role="status" className="text-[13px]">
          {checking ? (
            <Spinner />
          ) : plan?.people === 0 ? (
            <p className="m-0">Signals are up to date.</p>
          ) : running ? (
            <div className="grid gap-2">
              <div className="flex items-baseline justify-between gap-3">
                <span>{status.message}</span>
                {status.total ? (
                  <span className="tabular-nums text-muted-foreground">
                    {countOf(status.done, status.total, "request")}
                  </span>
                ) : null}
              </div>
              <ProgressBar label="Refresh progress" done={status.done} total={status.total} />
            </div>
          ) : status?.status === "completed" && !failure ? (
            <p className="m-0 text-ok">
              Signals refreshed for {plural((status.plan?.people ?? 0) - status.refused, "person")}.
              {status.refused
                ? ` Luna declined ${plural(status.refused, "person")}; they keep their old signals.`
                : null}
            </p>
          ) : null}
        </div>
        {failure && (
          <p role="alert" className="m-0 text-[13px] text-bad">
            {failure}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Close</Button>
          </DialogClose>
          {plan && plan.new_requests > 0 && (
            <Button
              variant="primary"
              disabled={checking}
              title={`Estimated $${plan.estimated_cost_usd.toFixed(4)}`}
              onClick={() => void confirm()}
            >
              Refresh for{" "}
              {plan.estimated_cost_usd < 0.01 ? "<$0.01" : `$${plan.estimated_cost_usd.toFixed(2)}`}
            </Button>
          )}
          {failure && !running && (
            <Button variant="primary" disabled={checking} onClick={() => void check()}>
              Check again
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
