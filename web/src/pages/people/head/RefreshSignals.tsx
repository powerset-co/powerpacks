import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Spinner } from "@/components/shared"
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

import { PEOPLE_QUERY_KEY } from "../hooks/usePeopleQuery"

const SIGNALS_KEY = ["people-signals"]

/** Refresh the exact selection, or every person, from saved dossiers and counts. */
export function RefreshSignals({
  people,
  selected,
  onDone,
}: {
  people: readonly string[]
  selected: boolean
  onDone: () => void
}) {
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
  const running = query.data?.status === "running"
  const start = async (ids: readonly string[], checked?: string) => {
    const status = await refreshSignals(ids, checked)
    client.setQueryData(SIGNALS_KEY, status)
    setPlan(null)
    if (status.status === "completed") {
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
    })
    const ids = [...people]
    setTargets(ids)
    try {
      await client.cancelQueries({ queryKey: SIGNALS_KEY })
      const next = await checkSignals(ids)
      if (next.new_requests === 0) await start(ids)
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
  const failure = error ?? query.error?.message ?? (query.data?.status === "failed" ? query.data.error : null)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          disabled={checking || !people.length}
          title={
            selected
              ? `Refresh signals for ${people.length} selected people`
              : `Refresh signals for all ${people.length} people`
          }
          onClick={() => (running ? setOpen(true) : void check())}
        >
          {running ? "Refreshing signals…" : "Refresh signals"}
          {running && <Spinner />}
        </Button>
      </DialogTrigger>
      <DialogContent className="gap-5 p-6" onKeyDown={(event) => event.stopPropagation()}>
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">Refresh signals</DialogTitle>
          <DialogDescription className="text-[13px]">
            {selected ? `${targets.length} selected people. ` : `All ${targets.length} people. `}
            Regenerate relationship, warmth and labels with JEV from saved dossiers and message counts. New
            requests send this saved evidence to TypeSafe/JEV.
          </DialogDescription>
        </DialogHeader>
        <div role="status" className="text-[13px]">
          {checking ? (
            <>
              <Spinner /> Checking saved signals…
            </>
          ) : plan ? (
            <p className="m-0">
              {plan.people.toLocaleString()} people · {plan.cached.toLocaleString()} cached answers ·{" "}
              {plan.new_requests.toLocaleString()} new requests.
              <br />
              Estimated cost: ${plan.estimated_cost_usd.toFixed(4)}. Confirm to make these paid requests.
            </p>
          ) : running ? (
            <>
              <Spinner /> {query.data?.message}
            </>
          ) : query.data?.status === "completed" && !failure ? (
            <p className="m-0 text-ok">
              Signals refreshed for {query.data.plan?.people.toLocaleString()} people.
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
          {plan && (
            <Button variant="primary" disabled={checking} onClick={() => void confirm()}>
              Confirm ${plan.estimated_cost_usd.toFixed(4)} refresh
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
