import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { errorText } from "@/lib/api/http"
import { readUpload, startUpload, type UploadAction, type UploadStatus } from "@/lib/api/upload"
import { sharedToast } from "@/lib/people/copy"
import { isActive, uploadView, type Refusal } from "@/lib/people/upload"

export const UPLOAD_KEY = ["people-upload"]
const POLL_MS = 1000

/**
 * The upload's status, polled every second while a check or upload runs and never otherwise.
 * A start the server refuses keeps its sentence until the next start or run; a failed poll
 * keeps the last status and says it is reconnecting.
 */
export function useUpload(onToast: (message: string) => void) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [starting, setStarting] = useState<UploadAction | null>(null)
  const [refused, setRefused] = useState<Refusal | null>(null)
  const query = useQuery({
    queryKey: UPLOAD_KEY,
    queryFn: async () => {
      const before = client.getQueryData<UploadStatus>(UPLOAD_KEY)
      const now = await readUpload()
      if (isActive(now.status)) setRefused(null)
      if (!open && before?.status === "uploading" && now.status === "completed") {
        onToast(sharedToast(now.progress.uploaded + now.progress.skipped))
      }
      return now
    },
    refetchInterval: (latest) => (latest.state.data && isActive(latest.state.data.status) ? POLL_MS : false),
    retry: false,
    staleTime: Infinity,
  })
  const status = query.data

  async function start(action: UploadAction, checked: string | null = null) {
    setStarting(action)
    setRefused(null)
    try {
      await client.cancelQueries({ queryKey: UPLOAD_KEY })
      client.setQueryData(UPLOAD_KEY, await startUpload(action, checked))
    } catch (error) {
      setRefused({ action, error: errorText(error) })
      // The run may have started anyway: read the status, which polls on if it did.
      void client.invalidateQueries({ queryKey: UPLOAD_KEY })
    } finally {
      setStarting(null)
    }
  }

  const busy = Boolean(starting) || (status !== undefined && isActive(status.status))

  /** Opening checks a network never checked, or one shared before; a check
   * still current opens on its plan, and a failure on its sentence. */
  function onOpen() {
    if (!busy && (!status || status.status === "idle" || status.status === "completed")) void start("check")
  }

  return {
    status,
    view: uploadView(status, starting, refused),
    busy,
    reconnecting: query.isError && busy,
    open,
    setOpen,
    onOpen,
    retry: () => void start("check"),
    // The confirm names the check it displayed, so a check from another tab cannot be confirmed here.
    confirm: () => void start("upload", status?.checked ?? null),
  }
}

export type Upload = ReturnType<typeof useUpload>
