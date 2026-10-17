import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState, type ReactNode, type SVGProps } from "react"
import { useNavigate } from "react-router-dom"

import { fetchAccount } from "@/lib/api/account"
import { signIn as authenticate } from "@/lib/api/feedback"
import { connectRelay } from "@/lib/api/relay"
import { errorText } from "@/lib/api/http"
import { useFeatures } from "@/lib/features"
import {
  checkUpdate,
  dismissedVersion,
  dismissVersion,
  installUpdate,
  lastCheck,
  onUpdateProgress,
  restartApp,
  UPDATE_INTERVAL_MS,
  type Update,
  type UpdateProgress,
} from "@/lib/api/update"
import { isDesktop } from "@/lib/desktop"
import { useDismiss } from "@/hooks/useDismiss"
import { cn } from "@/lib/utils"

import { Button } from "../ui/button"
import { AppFeedback } from "./AppFeedback"
import { Avatar } from "./Avatar"
import { CLOSE_MARK } from "./icons/actions"
import { SettingsIcon } from "./icons/nav"
import { ProgressBar } from "./ProgressBar"
import { RelayDot } from "./RelayDot"
import { Tip } from "./Tip"

const ACCOUNT_KEY = ["account"]
const UPDATE_KEY = ["update"]
// While signed out the footer watches for a sign-in that finishes on the server.
const SIGNED_OUT_POLL_MS = 5_000

/** The side nav's bottom: the update pane when a newer app is out, then who is signed in, whose
 *  row opens the account menu (Settings, Send feedback, Reconnect). */
export function SidebarFooter({ rail }: { rail: boolean }) {
  return (
    <div className="mt-auto flex flex-col">
      <UpdatePane rail={rail} />
      <Profile rail={rail} />
    </div>
  )
}

function useUpdate(): Update | null {
  const [checked] = useState(lastCheck)
  const query = useQuery({
    queryKey: UPDATE_KEY,
    queryFn: checkUpdate,
    enabled: isDesktop(),
    initialData: checked?.update,
    initialDataUpdatedAt: checked?.at,
    staleTime: UPDATE_INTERVAL_MS,
    refetchInterval: UPDATE_INTERVAL_MS,
    refetchIntervalInBackground: true,
    retry: false,
  })
  return query.data ?? null
}

/** The app's own version at the foot of the account menu, from the update check (the desktop app only). */
function Version() {
  const update = useUpdate()
  if (update === null) return null
  return (
    <span className="block px-2 pt-1.5 pb-0.5 text-[11px] text-faint tabular-nums" title="Powerpacks version">
      Powerpacks v{update.current}
    </span>
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
      <Version />
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
