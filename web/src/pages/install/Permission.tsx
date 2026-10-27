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
    <section
      className="relative m-2 flex flex-col items-center gap-2 rounded-[var(--radius-l)] border border-line-strong bg-card p-3 text-center shadow-2"
      aria-label="Messages permission"
    >
      <button
        type="button"
        className="absolute right-4 top-4 cursor-pointer border-0 bg-transparent p-0 text-sm leading-none text-muted-foreground hover:text-foreground"
        aria-label="Close"
        onClick={() => void invoke("permission_end").catch(fail)}
      >
        ×
      </button>
      {signed.data?.imessage === false ? (
        <>
          <button
            type="button"
            className="cursor-grab rounded-[var(--radius-m)] border-0 bg-transparent p-0 active:cursor-grabbing"
            aria-label="Drag Powerpacks into Full Disk Access"
            onMouseDown={(event) => {
              if (event.button !== 0) return
              event.preventDefault()
              setError(null)
              void invoke("permission_drag").catch(fail)
            }}
          >
            <img src={icon} alt="Powerpacks" width={56} height={56} draggable={false} />
          </button>
          <p className="m-0 text-xs">Drag this into the list, then turn it on.</p>
        </>
      ) : (
        <p className="m-0 text-xs">Checking Full Disk Access…</p>
      )}
      <div className="flex items-center gap-3">
        <Button
          size="sm"
          onClick={() => {
            setError(null)
            void installAction("permissions")
              .then(() => invoke("permission_begin"))
              .catch(fail)
          }}
        >
          Open the list again
        </Button>
        <Button size="sm" onClick={() => void invoke("permission_end").catch(fail)}>
          Skip
        </Button>
      </div>
      {error || signed.error ? (
        <p role="alert" className="m-0 max-h-10 overflow-y-auto text-xs text-bad">
          {error ?? errorText(signed.error)}
        </p>
      ) : null}
    </section>
  )
}
