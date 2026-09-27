import { useCallback, useState } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"

import { Toast, type ToastMessage } from "@/components/shared"

import { SearchesShell } from "./SearchesShell"
import { useCatalog } from "./hooks/useCatalog"
import { useFeedback } from "./hooks/useFeedback"
import { useSearchRun } from "./hooks/useSearchRun"
import { RunPane } from "./run/RunPane"
import { RunView } from "./run/RunView"
import { SearchSidebar } from "./sidebar/SearchSidebar"

export const RUN_PATH = "/searches/run"

/**
 * /searches (no run) and /searches/run?run_id=… (that run): one page, the URL picks the run.
 * The page owns the feedback queue (sent in order across runs) and the one toast every
 * control reports to.
 */
export function SearchesPage() {
  const [params] = useSearchParams()
  const runId = params.get("run_id")
  const navigate = useNavigate()
  const catalog = useCatalog()
  const feedback = useFeedback()
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const dismiss = useCallback(() => setToast(null), [])
  // Starts the picked run's request while the old pane fades out.
  useSearchRun(runId)

  const open = useCallback(
    (id: string) => {
      if (id !== runId) void navigate(`${RUN_PATH}?${new URLSearchParams({ run_id: id }).toString()}`)
    },
    [navigate, runId],
  )

  return (
    <>
      <SearchesShell
        sidebar={
          <SearchSidebar cards={catalog.data} error={catalog.error} selectedId={runId} onOpen={open} />
        }
        main={
          <RunPane
            runId={runId}
            renderRun={(payload) => (
              <RunView
                payload={payload}
                status={catalog.data?.find((card) => card.run_id === payload.search.run_id)?.status}
                feedback={feedback}
                onToast={setToast}
              />
            )}
          />
        }
      />
      <Toast toast={toast} onDismiss={dismiss} />
    </>
  )
}
