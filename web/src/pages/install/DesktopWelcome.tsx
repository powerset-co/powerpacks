import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"

import { ImportIcon, SparkIcon } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { importData, importSource } from "@/lib/api/install"
import { listen } from "@/lib/desktop"
import { HOME } from "@/lib/nav"
import { cn } from "@/lib/utils"

import { Connect } from "./Connect"
import { SetupKnot } from "./SetupKnot"

// The import's steps, as the app reports them done (desktop/src-tauri/src/lib.rs setup_import).
const IMPORT_STEPS = [
  "Find your Powerpacks install",
  "Pause the local app",
  "Bring over your network, imports and account",
  "Start the local app again",
]

function Heading({ title, line }: { title: string; line: string }) {
  return (
    <header className="flex flex-col items-center gap-1.5 text-center">
      <h2 className="m-0 text-xl font-bold">{title}</h2>
      <p className="m-0 text-sm text-muted-foreground">{line}</p>
    </header>
  )
}

const CHOICE =
  "group flex w-full cursor-pointer flex-col items-start gap-2.5 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left text-foreground transition-[border-color,background-color,transform,opacity] duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] hover:bg-surface-2 active:translate-y-px disabled:cursor-default disabled:hover:border-line-strong disabled:hover:bg-card disabled:active:translate-y-0"

/** A home folder path with the home part as a tilde, the way people write it. */
function tilde(path: string): string {
  return path.replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, "~")
}

/** Import existing data directly; only a new setup needs the prerequisite check. */
function Choice({
  onStart,
  onImport,
  onImported,
}: {
  onStart: () => void
  onImport?: (active: boolean) => void
  onImported: () => void
}) {
  const client = useQueryClient()
  const [clicked, setClicked] = useState<"import" | null>(null)
  const [error, setError] = useState<string | null>(null)
  // How many of the import's steps are done.
  const [done, setDone] = useState(0)
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
    setDone(0)
    onImport?.(true)
    const stop = listen("import://step", (count) => setDone(Number(count)))
    task().catch(fail).finally(stop)
  }
  if (chosen === "import") {
    return (
      <div role="status" aria-live="polite" className="rise-in flex w-full flex-col items-center gap-5">
        <Heading title="Bringing over your data" line={`${done} of ${IMPORT_STEPS.length} tasks completed`} />
        <ol className="install-steps m-0 w-full max-w-[420px] p-0" aria-label="Import steps">
          {IMPORT_STEPS.map((label, index) => {
            const finished = index < done
            const current = index === done
            return (
              <li key={label} data-done={finished} aria-current={current ? "step" : undefined}>
                <div className="install-step-row">
                  <span className="install-step-mark" aria-hidden="true">
                    {finished ? "✓" : current ? "•" : "○"}
                  </span>
                  <span>{label}</span>
                  <span className="install-step-status">
                    {finished ? "Done" : current ? "Working" : "Next"}
                  </span>
                </div>
              </li>
            )
          })}
        </ol>
        {source ? <p className="m-0 text-xs text-muted-foreground">{tilde(source)}</p> : null}
      </div>
    )
  }
  return (
    <div className="flex w-full flex-col gap-4">
      <Heading title="Setup" line="Start fresh, or bring over what you already have on this Mac." />
      <div className="grid w-full grid-cols-2 gap-3 max-[720px]:grid-cols-1">
        <button type="button" className={CHOICE} onClick={onStart}>
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-primary-soft text-primary">
            <SparkIcon className="size-[18px]" />
          </span>
          <span className="text-[13px] font-bold">Start setup</span>
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
              await importData()
              await client.invalidateQueries()
              onImported()
            })
          }
          data-import-source={source ?? ""}
        >
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground group-hover:text-primary">
            <ImportIcon className="size-[18px]" />
          </span>
          <span className="text-[13px] font-bold">Import existing data</span>
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
 *  The third screen, the installation status, is the install page itself once setup runs. An
 *  import ends on the same Connect list, for what did not come over, with no setup to run. */
export function DesktopWelcome({ onImport }: { onImport?: (active: boolean) => void }) {
  const navigate = useNavigate()
  const [stage, setStage] = useState<"choose" | "connect" | "imported">("choose")
  return (
    <div className="flex w-full flex-col items-center gap-6">
      <div key={stage} className="rise-in flex w-full flex-col items-center gap-5">
        {/* The knot greets a new install and stays through an import; connecting is a plain list. */}
        {stage === "choose" ? (
          <>
            <SetupKnot />
            <Choice
              onStart={() => setStage("connect")}
              onImport={onImport}
              onImported={() => setStage("imported")}
            />
          </>
        ) : stage === "connect" ? (
          <>
            <Heading title="Setup" line="Connect your accounts. Setup does the rest on its own." />
            <Connect onBack={() => setStage("choose")} />
          </>
        ) : (
          <>
            <Heading title="Setup" line="Your data is here. Connect anything that didn't come over." />
            <Connect onDone={() => void navigate(HOME.href)} />
          </>
        )}
      </div>
    </div>
  )
}
