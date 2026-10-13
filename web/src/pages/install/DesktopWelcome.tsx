import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState } from "react"

import { useNavigate } from "react-router-dom"

import { ImportIcon, SparkIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { importData, importSource } from "@/lib/api/install"
import { EmptyPanel } from "@/pages/review/shared/EmptyPanel"
import { HOME } from "@/lib/nav"
import { cn } from "@/lib/utils"

import { Connect } from "./Connect"
import { SetupKnot } from "./SetupKnot"

const CHOICE =
  "group flex w-full cursor-pointer flex-col items-start gap-2.5 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left text-foreground transition-[border-color,background-color,transform,opacity] duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] hover:bg-surface-2 active:translate-y-px disabled:cursor-default disabled:hover:border-line-strong disabled:hover:bg-card disabled:active:translate-y-0"

/** A home folder path with the home part as a tilde, the way people write it. */
function tilde(path: string): string {
  return path.replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, "~")
}

/** Import existing data directly; only a new setup needs the prerequisite check. */
function Choice({ onStart, onImport }: { onStart: () => void; onImport?: (active: boolean) => void }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [clicked, setClicked] = useState<"import" | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [imported, setImported] = useState<boolean | null>(null)
  const chosen = error ? null : clicked
  // undefined while the app looks; null when there is nothing to import.
  const [source, setSource] = useState<string | null | undefined>(undefined)
  const fail = useCallback((caught: unknown) => setError(errorText(caught)), [])
  useEffect(() => {
    importSource()
      .then(setSource)
      .catch((caught: unknown) => {
        setSource(null)
        fail(caught)
      })
  }, [fail])
  const choose = (task: () => Promise<void>) => {
    setError(null)
    setClicked("import")
    onImport?.(true)
    task().catch(fail)
  }
  if (chosen === "import") {
    return (
      <div role="status" aria-live="polite" className="w-full rise-in">
        <EmptyPanel
          title={imported === null ? "Connecting your existing data" : "Existing data connected"}
          mark={imported !== null}
          above={imported === null ? <Spinner className="size-6 text-primary" /> : undefined}
        >
          <p className="text-sm text-muted-foreground">
            {imported === null
              ? "Connecting your network and restarting the local app. This can take a few seconds."
              : "The app now shares this install’s network, imports and account."}
          </p>
          <p className="text-xs text-muted-foreground">{source && tilde(source)}</p>
          {imported !== null && (
            <Button
              variant="primary"
              onClick={() => {
                if (imported) void navigate(HOME.href)
                else onImport?.(false)
              }}
            >
              {imported ? "Open People" : "Continue setup"}
            </Button>
          )}
        </EmptyPanel>
      </div>
    )
  }
  return (
    <div className="flex w-full flex-col gap-4">
      <div className="grid w-full grid-cols-2 gap-3 max-[720px]:grid-cols-1">
        <button type="button" className={CHOICE} onClick={onStart}>
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-primary-soft text-primary">
            <SparkIcon className="size-[18px]" />
          </span>
          <span className="text-[13.5px] font-bold">Start setup</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Sign in, import your contacts and build your network from scratch.
          </span>
        </button>
        <button
          type="button"
          className={cn(
            CHOICE,
            source && "border-[color-mix(in_srgb,var(--primary)_35%,var(--line-strong))]",
            !source && "opacity-40",
          )}
          disabled={!source}
          onClick={() =>
            choose(async () => {
              const done = await importData()
              await client.invalidateQueries()
              setImported(done)
            })
          }
          data-import-source={source ?? ""}
        >
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground group-hover:text-primary">
            <ImportIcon className="size-[18px]" />
          </span>
          <span className="text-[13.5px] font-bold">Import existing data</span>
          <span className="text-xs leading-snug text-muted-foreground">
            {source === undefined
              ? "Looking for a Powerpacks install on this computer…"
              : source === null
                ? "No command-line Powerpacks install found on this computer."
                : `Use the network, imports and account already in ${tilde(source)}.`}
          </span>
        </button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}

/** The desktop app's setup before it starts: import, or connect every account for a new setup.
 *  The third screen, the installation status, is the install page itself once setup runs. */
export function DesktopWelcome({ onImport }: { onImport?: (active: boolean) => void }) {
  const [stage, setStage] = useState<0 | 1>(0)
  return (
    <div className="flex w-full flex-col items-center gap-6">
      <div key={stage} className="rise-in flex w-full flex-col items-center gap-5">
        {/* The knot greets a new install; connecting is a plain list of sign-ins. */}
        {stage === 0 ? <SetupKnot /> : null}
        <header className="flex flex-col items-center gap-1.5 text-center">
          <h2 className="m-0 text-xl font-bold">Setup</h2>
          <p className="m-0 text-sm text-muted-foreground">
            {stage === 0
              ? "Start fresh, or bring over what you already have on this Mac."
              : "Connect your accounts. Setup does the rest on its own."}
          </p>
        </header>
        {stage === 0 ? (
          <Choice onStart={() => setStage(1)} onImport={onImport} />
        ) : (
          <Connect onBack={() => setStage(0)} />
        )}
      </div>
    </div>
  )
}
