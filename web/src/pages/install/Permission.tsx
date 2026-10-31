import { ArrowUp, X } from "lucide-react"
import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import icon from "../../../../desktop/src-tauri/icons/128x128.png"
import { Button } from "@/components/ui/button"
import { fetchSignedIn } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { installAction } from "@/lib/api/install"
import { invoke } from "@/lib/desktop"
import { POLL_MS } from "./sources"

export function Permission() {
  const [error, setError] = useState<string | null>(null)
  const signed = useQuery({
    queryKey: ["signed-in"],
    queryFn: ({ signal }) => fetchSignedIn(signal),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: true,
  })
  const fail = (caught: unknown) => setError(errorText(caught))
  useEffect(() => {
    if (signed.data?.imessage) void invoke("permission_end").catch(fail)
  }, [signed.data?.imessage])
  return (
    <div className="flex h-dvh items-center p-3">
      <section
        className="flex w-full flex-col gap-2 rounded-2xl border border-line-strong bg-card p-2 shadow-2"
        aria-label="Messages permission"
      >
        <div className="flex min-h-8 items-center gap-2 pl-10">
          <ArrowUp size={24} className="shrink-0 text-primary" aria-hidden="true" />
          <p className="m-0 flex-1 text-xs text-muted-foreground">
            Drag Powerpacks into the list, then turn it on.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="flex size-8 shrink-0 items-center justify-center rounded-full border-0 bg-surface-2 text-muted-foreground hover:text-foreground"
            aria-label="Skip"
            onClick={() => void invoke("permission_end").catch(fail)}
          >
            <X size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="flex min-h-10 min-w-0 flex-1 cursor-grab items-center gap-2 rounded-xl border border-line-strong bg-card px-2 py-1 text-left text-xs active:cursor-grabbing"
            aria-label="Drag Powerpacks into Full Disk Access"
            onMouseDown={(event) => {
              if (event.button !== 0) return
              event.preventDefault()
              setError(null)
              void invoke("permission_drag").catch(fail)
            }}
          >
            <img src={icon} alt="Powerpacks" width={24} height={24} draggable={false} />
            Powerpacks
          </button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setError(null)
              void installAction("permissions")
                .then(() => invoke("permission_begin"))
                .catch(fail)
            }}
          >
            Open the list again
          </Button>
        </div>
        {error || signed.error ? (
          <p role="alert" className="m-0 max-h-8 overflow-y-auto pl-10 text-xs text-bad">
            {error ?? errorText(signed.error)}
          </p>
        ) : null}
      </section>
    </div>
  )
}
