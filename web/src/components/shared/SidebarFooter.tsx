import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState, type ReactNode, type SVGProps } from "react"
import { useNavigate } from "react-router-dom"

import { ACCOUNT_KEY, fetchAccount } from "@/lib/api/account"
import { fetchInstall } from "@/lib/api/install"
import { useSetupDone } from "@/hooks/useSetupDone"
import { isDesktop } from "@/lib/desktop"
import { readStored, writeStored } from "@/lib/storage"
import { signIn as authenticate } from "@/lib/api/feedback"
import { connectRelay } from "@/lib/api/relay"
import { isRecord } from "@/lib/utils"
import { errorText } from "@/lib/api/http"
import { useFeatures } from "@/lib/features"
import type { InstallStatus } from "@/types/install"
import {
  dismissedVersion,
  dismissVersion,
  installUpdate,
  onUpdateProgress,
  restartApp,
  type UpdateProgress,
} from "@/lib/api/update"
import { useUpdate } from "@/hooks/useUpdate"
import { useDismiss } from "@/hooks/useDismiss"
import { cn } from "@/lib/utils"

import { Button } from "../ui/button"
import { AppFeedback } from "./AppFeedback"
import { Avatar } from "./Avatar"
import { CheckIcon, CLOSE_MARK } from "./icons/actions"
import { CirclePlus } from "lucide-react"
import { SettingsIcon, SetupIcon } from "./icons/nav"
import { ProgressBar } from "./ProgressBar"
import { RelayDot } from "./RelayDot"
import { Spinner } from "./Spinner"
import { Tip } from "./Tip"

// While signed out the footer watches for a sign-in that finishes on the server.
const SIGNED_OUT_POLL_MS = 5_000

/** The side nav's bottom: the import pane while an import shows, otherwise the sources prompt
 *  (never both), the update pane when a newer app is out, then who is signed in, whose row opens
 *  the account menu (Settings, Send feedback, Reconnect). */
export function SidebarFooter({ rail }: { rail: boolean }) {
  const run = useImport()
  return (
    <div className="mt-auto flex flex-col">
      {run ? <ImportPane rail={rail} {...run} /> : <SourcesPane rail={rail} />}
      <UpdatePane rail={rail} />
      <Profile rail={rail} />
    </div>
  )
}

// The finished run whose pane was dismissed (its record's updated_at); a later run shows again.
const IMPORT_DISMISSED_KEY = "import-pane-dismissed"
const IMPORT_POLL_MS = 5_000
// Accounts owns later imports and their progress.
const IMPORT_HREF = "/accounts?view=sync"

/** The setup run the import pane shows, with its dismiss; null once a finished run is dismissed
 *  (or before any run). No dismissing while it runs or is stopped, so the way back is always there. */
function useImport(): { data: InstallStatus; dismiss: () => void } | null {
  const install = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    refetchInterval: IMPORT_POLL_MS,
    retry: false,
  })
  const [dismissed, setDismissed] = useState(() =>
    readStored("local", IMPORT_DISMISSED_KEY, (raw) => (typeof raw === "string" ? raw : null)),
  )
  const data = install.data
  if (data === undefined || data.event === "setup.ready" || (finished(data) && dismissed === data.updated_at))
    return null
  const dismiss = () => {
    writeStored("local", IMPORT_DISMISSED_KEY, data.updated_at)
    setDismissed(data.updated_at)
  }
  return { data, dismiss }
}

function finished(data: InstallStatus): boolean {
  return data.status === "completed" && data.step === "ready"
}

/** Import: the setup run's line, opening the Import screen; once it reaches ready, a dismiss. */
function ImportPane({ rail, data, dismiss }: { rail: boolean; data: InstallStatus; dismiss: () => void }) {
  const navigate = useNavigate()
  const done = finished(data)
  const stopped = data.status === "failed"
  const title = done ? "Import done" : stopped ? "Import stopped" : "Importing"
  const counts = isRecord(data.action?.details) ? data.action.details : {}
  const total = typeof counts.total === "number" ? counts.total : 0
  const count = counts.done ?? counts.read
  const completed = typeof count === "number" ? count : 0
  const open = () => void navigate(IMPORT_HREF)
  const icon = done ? (
    <CheckIcon className="size-4 shrink-0 text-ok" />
  ) : stopped ? (
    <SetupIcon className="size-4 shrink-0 text-bad" />
  ) : (
    <span className="grid size-4 shrink-0 place-items-center">
      <Spinner className="size-3.5 text-primary" />
    </span>
  )
  if (rail) {
    return (
      <div className="flex justify-center pb-1">
        <button
          type="button"
          aria-label={`${title}: ${data.message}`}
          onClick={open}
          className="group relative grid size-9 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 transition-colors duration-fast ease-out hover:bg-secondary"
        >
          {icon}
          <Tip side="right">{data.message}</Tip>
        </button>
      </div>
    )
  }
  return (
    <div className="mx-2 mb-2 rounded-[var(--radius-m)] border border-line bg-[color-mix(in_srgb,var(--surface-2)_70%,transparent)] p-2.5">
      <div className="flex items-start gap-2">
        <span className="mt-px">{icon}</span>
        <span className="min-w-0 flex-1 leading-[1.25]">
          <span className="block truncate text-[12px] font-semibold text-foreground">{title}</span>
          <span className="line-clamp-2 block text-[11px] text-muted-foreground">{data.message}</span>
        </span>
        {done ? (
          <button
            type="button"
            aria-label="Dismiss this import"
            onClick={dismiss}
            className="-mt-1 -mr-1 grid size-6 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-[15px] leading-none text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
          >
            {CLOSE_MARK}
          </button>
        ) : null}
      </div>
      {!done && !stopped ? (
        <div className="mt-2">
          <ProgressBar label="Current import stage" done={completed} total={total} />
        </div>
      ) : null}
      <button
        type="button"
        onClick={open}
        className="mt-2 border-0 bg-transparent p-0 text-[11px] text-primary hover:underline"
      >
        View in Accounts
      </button>
    </div>
  )
}

// Remembered on this machine once dismissed; a message import ends it for good.
const SOURCES_PROMPT_KEY = "sources-prompt-dismissed"
const MESSAGE_IMPORTS = ["gmail_import", "imessage_import", "whatsapp_import"]

/** Add sources: once setup is done with LinkedIn alone, a prompt for Gmail, Messages and WhatsApp,
 *  opening Accounts. Dismissable; gone once a message import has run. */
function SourcesPane({ rail }: { rail: boolean }) {
  const navigate = useNavigate()
  const done = useSetupDone()
  const install = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    enabled: isDesktop() && done,
    retry: false,
  })
  const [dismissed, setDismissed] = useState(
    () => readStored("local", SOURCES_PROMPT_KEY, (raw) => (raw === true ? true : null)) ?? false,
  )
  const imported = MESSAGE_IMPORTS.some((step) => install.data?.steps[step]?.status === "completed")
  if (!done || dismissed || install.data === undefined || imported) return null
  const open = () => void navigate("/accounts")
  const dismiss = () => {
    writeStored("local", SOURCES_PROMPT_KEY, true)
    setDismissed(true)
  }
  if (rail) {
    return (
      <div className="flex justify-center pb-1">
        <button
          type="button"
          aria-label="Add sources"
          onClick={open}
          className="group relative grid size-9 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-primary transition-colors duration-fast ease-out hover:bg-secondary"
        >
          <CirclePlus className="size-[18px]" />
          <Tip side="right">Add sources</Tip>
        </button>
      </div>
    )
  }
  return (
    <div className="mx-2 mb-2 rounded-[var(--radius-m)] border border-line bg-[color-mix(in_srgb,var(--surface-2)_70%,transparent)] p-2.5">
      <div className="flex items-start gap-2">
        <CirclePlus className="mt-px size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 leading-[1.25]">
          <span className="block truncate text-[12px] font-semibold text-foreground">Add more sources</span>
          <span className="block text-[11px] text-muted-foreground">
            Gmail, Messages and WhatsApp add the people you talk to.
          </span>
        </span>
        <button
          type="button"
          aria-label="Dismiss this prompt"
          onClick={dismiss}
          className="-mt-1 -mr-1 grid size-6 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-[15px] leading-none text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
        >
          {CLOSE_MARK}
        </button>
      </div>
      <Button variant="primary" size="sm" onClick={open} className="mt-2.5 w-full">
        Add sources
      </Button>
    </div>
  )
}

// Before the download's first bytes arrive.
const NO_PROGRESS: UpdateProgress = { downloaded: 0, total: null }

/** "Downloading… 42%" (no share while the size is unknown), then "Installing…" once every byte is in. */
function installStep(progress: UpdateProgress): string {
  if (progress.total === null) return "Downloading…"
  if (progress.downloaded >= progress.total) return "Installing…"
  return `Downloading… ${Math.floor((progress.downloaded / progress.total) * 100)}%`
}

/** Install update downloads and installs the new version in place; Restart now runs it, Later
 *  leaves it for the next launch. A failed install shows why and offers Install update again. */
function UpdatePane({ rail }: { rail: boolean }) {
  const update = useUpdate()
  const [dismissed, setDismissed] = useState(dismissedVersion)
  const [progress, setProgress] = useState(NO_PROGRESS)
  const install = useMutation({
    mutationFn: async () => {
      setProgress(NO_PROGRESS)
      const stop = onUpdateProgress(setProgress)
      try {
        await installUpdate()
      } finally {
        stop()
      }
    },
  })
  if (update === null || !update.available || update.latest === dismissed) return null
  const dismiss = () => {
    dismissVersion(update.latest)
    setDismissed(update.latest)
  }
  const problem = install.error ? errorText(install.error) : null
  if (rail) {
    const label = install.isSuccess
      ? "Restart to update"
      : install.isPending
        ? installStep(progress)
        : (problem ?? `Update available · v${update.latest}`)
    const click = () => {
      if (install.isSuccess) void restartApp()
      else if (!install.isPending) install.mutate()
    }
    return (
      <div className="flex justify-center pb-1">
        <button
          type="button"
          aria-label={install.isSuccess || install.isPending ? label : `${label}. Install update`}
          onClick={click}
          className="group relative grid size-9 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-primary transition-colors duration-fast ease-out hover:bg-secondary"
        >
          <UpdateIcon className="size-[18px]" />
          <Tip side="right">{label}</Tip>
        </button>
      </div>
    )
  }
  return (
    <div className="mx-2 mb-2 rounded-[var(--radius-m)] border border-line bg-[color-mix(in_srgb,var(--surface-2)_70%,transparent)] p-2.5">
      <div className="flex items-start gap-2">
        <UpdateIcon className="mt-px size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 leading-[1.25]">
          <span className="block truncate text-[12px] font-semibold text-foreground">Update available</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            v{update.latest} · from v{update.current}
          </span>
        </span>
        {install.isIdle || install.isError ? (
          <button
            type="button"
            aria-label="Dismiss this update"
            onClick={dismiss}
            className="-mt-1 -mr-1 grid size-6 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-[15px] leading-none text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
          >
            {CLOSE_MARK}
          </button>
        ) : null}
      </div>
      {install.isPending ? (
        <div className="mt-2.5 flex flex-col gap-1.5">
          <ProgressBar
            label="Downloading the update"
            done={progress.downloaded}
            total={progress.total ?? 0}
          />
          <span className="text-[11px] text-muted-foreground">{installStep(progress)}</span>
        </div>
      ) : install.isSuccess ? (
        <div className="mt-2.5 flex items-center gap-1.5">
          <Button variant="primary" size="sm" onClick={() => void restartApp()} className="flex-1">
            Restart now
          </Button>
          <Button variant="ghost" size="sm" onClick={dismiss}>
            Later
          </Button>
        </div>
      ) : (
        <>
          {problem !== null && (
            <p role="alert" className="mt-2 mb-0 text-[11px] break-words text-bad">
              {problem}
            </p>
          )}
          <Button variant="primary" size="sm" onClick={() => install.mutate()} className="mt-2.5 w-full">
            Install update
          </Button>
        </>
      )}
    </div>
  )
}

const ITEM =
  "flex w-full cursor-pointer items-center rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:bg-secondary disabled:cursor-default disabled:opacity-50"

function MenuItem({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void
  disabled?: boolean
  children: ReactNode
}) {
  return (
    <button type="button" role="menuitem" className={ITEM} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  )
}

function Profile({ rail }: { rail: boolean }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const features = useFeatures()
  const account = useQuery({
    queryKey: ACCOUNT_KEY,
    queryFn: ({ signal }) => fetchAccount(signal),
    refetchInterval: (query) => (query.state.data?.signed_in ? false : SIGNED_OUT_POLL_MS),
  })
  const signIn = useMutation({
    mutationFn: async () => {
      await authenticate()
      await connectRelay()
    },
    onSettled: () => client.invalidateQueries({ queryKey: ACCOUNT_KEY }),
  })
  const [menu, setMenu] = useState(false)
  const [feedback, setFeedback] = useState(false)
  const anchor = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const close = useCallback(() => setMenu(false), [])
  useDismiss(menu, panel, anchor, close)

  const data = account.data
  const signedIn = data?.signed_in === true
  const name = signedIn ? (data.name ?? data.email) : null
  const email = signedIn && data.name !== null ? data.email : null
  const picture = signedIn ? data.picture : null
  const busy = signIn.isPending
  const problem = signIn.error ? errorText(signIn.error) : null
  const title = data === undefined ? "Checking who is signed in…" : signedIn ? name : "Not signed in"
  const detail = problem ?? email ?? (signedIn || data === undefined ? null : "Powerset")

  // The relay carries the sets' messages: no Sets, no relay to show.
  const face = (
    <span className="relative shrink-0">
      <Avatar
        name={name}
        src={picture}
        className={cn(
          "size-8",
          signedIn &&
            "border-0 bg-gradient-to-br from-[#f2502a] to-[#f87171] text-[12px] font-semibold text-white",
        )}
      />
      {features.sets ? <RelayDot tip={rail ? "right" : "above"} /> : null}
    </span>
  )
  const items = menu && (
    <div
      ref={panel}
      role="menu"
      aria-label="Account"
      className={cn(
        "rise-in absolute z-50 rounded-[var(--radius-m)] border border-line-strong bg-card p-1.5 shadow-[var(--shadow-2)]",
        rail ? "bottom-2 left-full ml-2 w-[200px]" : "inset-x-2 bottom-full mb-1",
      )}
    >
      <MenuItem
        onClick={() => {
          close()
          void navigate("/settings")
        }}
      >
        Settings
      </MenuItem>
      <MenuItem
        onClick={() => {
          close()
          setFeedback(true)
        }}
      >
        Send feedback
      </MenuItem>
      <div className="my-1.5 border-t border-line" />
      <MenuItem
        disabled={busy || data === undefined}
        onClick={() => {
          close()
          signIn.mutate()
        }}
      >
        {busy ? "Signing in…" : signedIn ? "Reconnect Powerset" : "Sign in to Powerset"}
      </MenuItem>
    </div>
  )
  const trigger = {
    ref: anchor,
    type: "button" as const,
    "aria-label": "Account",
    "aria-expanded": menu,
    onClick: () => setMenu(!menu),
  }
  if (rail) {
    return (
      <div className="relative flex justify-center border-t border-line py-2.5">
        <button
          {...trigger}
          className="group relative block cursor-pointer rounded-full border-0 bg-transparent p-0 hover:opacity-80"
        >
          {face}
          {menu ? null : <Tip side="right">{problem ?? title}</Tip>}
        </button>
        {items}
        {problem && (
          <span role="alert" className="sr-only">
            {problem}
          </span>
        )}
        <AppFeedback open={feedback} onOpenChange={setFeedback} />
      </div>
    )
  }
  return (
    <div className="relative flex items-center gap-2 border-t border-line py-2 pr-2 pl-2">
      <button
        {...trigger}
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5 rounded-[var(--radius-s)] border-0 bg-transparent px-1 py-0.5 text-left transition-colors duration-fast ease-out hover:bg-secondary aria-expanded:bg-secondary"
      >
        {face}
        <span className="min-w-0 flex-1 leading-[1.25]">
          {/* One line each: an email standing in for the name is set smaller and lighter to fit. */}
          <span
            className={cn(
              "block truncate text-foreground",
              signedIn && data.name === null ? "text-[12px] font-medium" : "text-[13px] font-semibold",
            )}
            title={title ?? undefined}
          >
            {title}
          </span>
          {detail !== null && (
            <span
              className={cn(
                "block text-[11px] text-muted-foreground",
                problem === null ? "truncate" : "text-bad",
              )}
              title={problem === null ? detail : undefined}
              role={problem ? "alert" : undefined}
            >
              {detail}
            </span>
          )}
        </span>
        <SettingsIcon className="size-4 shrink-0 text-faint" />
      </button>
      {data !== undefined && !signedIn && (
        <Button size="sm" onClick={() => signIn.mutate()} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      )}
      {items}
      <AppFeedback open={feedback} onOpenChange={setFeedback} />
    </div>
  )
}

function UpdateIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...props}
    >
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5v8M8.5 12l3.5 3.5 3.5-3.5" />
    </svg>
  )
}
