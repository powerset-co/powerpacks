import { useCallback, useState } from "react"

import { Toast, type ToastMessage } from "@/components/shared"

import { useCatalog } from "./hooks/useCatalog"
import { useFeedback } from "./hooks/useFeedback"
import { RunPane } from "./run/RunPane"
import { RunView } from "./run/RunView"
import "./styles/shell.css"
import "./styles/run.css"
import "./styles/results.css"
import "./styles/toolbar.css"

/** One saved run with its controls, feedback queue and toast: the Searches page's main pane,
 *  and the results beside a chat that ran a search. */
export function RunColumn({ runId }: { runId: string | null }) {
  const catalog = useCatalog()
  const feedback = useFeedback(runId)
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const dismiss = useCallback(() => setToast(null), [])
  return (
    <>
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
      <Toast toast={toast} onDismiss={dismiss} />
    </>
  )
}
