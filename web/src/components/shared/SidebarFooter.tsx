import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type SVGProps } from "react"

import { fetchAccount, POWERSET_CALLBACK, startSignIn } from "@/lib/api/account"
import { signIn as signInInBrowser } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import { openExternal } from "@/lib/api/install"
import {
  checkUpdate,
  dismissedVersion,
  dismissVersion,
  lastCheck,
  UPDATE_INTERVAL_MS,
  type Update,
} from "@/lib/api/update"
import { isDesktop } from "@/lib/desktop"
import { openSignIn, useSignIn } from "@/lib/signin"
import { cn } from "@/lib/utils"

import { Button } from "../ui/button"
import { Avatar } from "./Avatar"
import { CLOSE_MARK } from "./icons/actions"
import { RelayDot } from "./RelayDot"
import { Tip } from "./Tip"

const ACCOUNT_KEY = ["account"]
const UPDATE_KEY = ["update"]
// While signed out the footer watches for the sign-in the server finishes after the pane closes.
const SIGNED_OUT_POLL_MS = 5_000

/** The side nav's bottom: the update pane when a newer app is out, then who is signed in. */
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

function UpdatePane({ rail }: { rail: boolean }) {
  const update = useUpdate()
  const [dismissed, setDismissed] = useState(dismissedVersion)
  if (update === null || !update.available || update.latest === dismissed) return null
  const label = `Update available · v${update.latest}`
  const get = () => void openExternal(update.url)
  if (rail) {
    return (
      <div className="flex justify-center pb-1">
        <button
          type="button"
          aria-label={`${label}. Get update`}
          onClick={get}
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
        <button
          type="button"
          aria-label="Dismiss this update"
          onClick={() => {
            dismissVersion(update.latest)
            setDismissed(update.latest)
          }}
          className="-mt-1 -mr-1 grid size-6 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-[16px] leading-none text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
        >
          {CLOSE_MARK}
        </button>
      </div>
      <Button variant="primary" size="sm" onClick={get} className="mt-2.5 w-full">
        Get update
      </Button>
    </div>
  )
}

function Profile({ rail }: { rail: boolean }) {
  const client = useQueryClient()
  const account = useQuery({
    queryKey: ACCOUNT_KEY,
    queryFn: ({ signal }) => fetchAccount(signal),
    refetchInterval: (query) => (query.state.data?.signed_in ? false : SIGNED_OUT_POLL_MS),
  })
  const signIn = useMutation({
    mutationFn: async () => {
      if (isDesktop()) openSignIn({ title: "Powerset", url: await startSignIn(), finish: POWERSET_CALLBACK })
      else await signInInBrowser()
    },
    onSettled: () => client.invalidateQueries({ queryKey: ACCOUNT_KEY }),
  })
  // The desktop pane closing is the sign-in finishing: read the account again.
  const pane = useSignIn()
  const paneWasOpen = useRef(false)
  useEffect(() => {
    if (pane !== null) paneWasOpen.current = true
    else if (paneWasOpen.current) {
      paneWasOpen.current = false
      void client.invalidateQueries({ queryKey: ACCOUNT_KEY })
    }
  }, [pane, client])

  const data = account.data
  const signedIn = data?.signed_in === true
  const name = signedIn ? (data.name ?? data.email) : null
  const email = signedIn && data.name !== null ? data.email : null
  const busy = signIn.isPending || pane !== null
  const problem = signIn.error ? errorText(signIn.error) : null
  const title = data === undefined ? "Checking who is signed in…" : signedIn ? name : "Not signed in"
  const detail = problem ?? email ?? (signedIn || data === undefined ? null : "Powerset")

  // The relay dot is the avatar's sibling, never inside it: each keeps its own hover label.
  const relay = <RelayDot tip={rail ? "right" : "above"} />
  if (rail) {
    const tip = signedIn ? [title, email].filter((part) => part !== null).join(" · ") : `${title} · Sign in`
    return (
      <div className="flex justify-center border-t border-line py-2.5">
        <span className="relative">
          {signedIn ? (
            <span className="group relative block" aria-label={tip}>
              <Avatar name={name} />
              <Tip side="right">{tip}</Tip>
            </span>
          ) : (
            <button
              type="button"
              aria-label={tip}
              disabled={busy || data === undefined}
              onClick={() => signIn.mutate()}
              className="group relative block cursor-pointer rounded-full border-0 bg-transparent p-0 hover:[&>span]:border-foreground/60 hover:[&>span]:text-foreground disabled:cursor-default"
            >
              <Avatar name={null} />
              <Tip side="right">{busy ? "Signing in…" : tip}</Tip>
            </button>
          )}
          {relay}
        </span>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2.5 border-t border-line py-2.5 pr-2 pl-3">
      <span className="relative shrink-0">
        <Avatar name={name} />
        {relay}
      </span>
      <div className="min-w-0 flex-1 leading-[1.25]">
        <div className="truncate text-[13px] font-semibold text-foreground">{title}</div>
        {detail !== null && (
          <div
            className={cn("truncate text-[11px] text-muted-foreground", problem !== null && "text-bad")}
            title={detail}
          >
            {detail}
          </div>
        )}
      </div>
      {data !== undefined && !signedIn && (
        <Button size="sm" onClick={() => signIn.mutate()} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      )}
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
