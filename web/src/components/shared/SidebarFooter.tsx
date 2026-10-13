import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type SVGProps } from "react"

import { fetchAccount } from "@/lib/api/account"
import { signIn as authenticate } from "@/lib/api/feedback"
import { connectRelay } from "@/lib/api/relay"
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
import { useSignIn } from "@/lib/signin"
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
      await authenticate()
      await connectRelay()
    },
    onSettled: () => client.invalidateQueries({ queryKey: ACCOUNT_KEY }),
  })
  const pane = useSignIn()

  const data = account.data
  const signedIn = data?.signed_in === true
  const name = signedIn ? (data.name ?? data.email) : null
  const email = signedIn && data.name !== null ? data.email : null
  const picture = signedIn ? data.picture : null
  const busy = signIn.isPending || pane !== null
  const problem = signIn.error ? errorText(signIn.error) : null
  const title = data === undefined ? "Checking who is signed in…" : signedIn ? name : "Not signed in"
  const detail = problem ?? email ?? (signedIn || data === undefined ? null : "Powerset")

  const label = signedIn ? "Reconnect Powerset" : "Sign in to Powerset"
  const avatar = (
    <button
      type="button"
      aria-label={label}
      disabled={busy || data === undefined}
      onClick={() => signIn.mutate()}
      className={cn(
        "group relative block cursor-pointer rounded-full border-0 bg-transparent p-0 disabled:cursor-default",
        signedIn
          ? "hover:[&>span]:opacity-80"
          : "hover:[&>span]:border-foreground/60 hover:[&>span]:text-foreground",
      )}
    >
      <Avatar
        name={name}
        src={picture}
        className={cn(
          "size-8",
          signedIn &&
            "border-0 bg-gradient-to-br from-[#f2502a] to-[#f87171] text-[12px] font-semibold text-white",
        )}
      />
      <Tip side={rail ? "right" : "above"}>{problem ?? (busy ? "Signing in…" : label)}</Tip>
    </button>
  )
  const relay = <RelayDot tip={rail ? "right" : "above"} />
  if (rail) {
    return (
      <div className="flex justify-center border-t border-line py-2.5">
        <span className="relative">
          {avatar}
          {relay}
        </span>
        {problem && (
          <span role="alert" className="sr-only">
            {problem}
          </span>
        )}
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2.5 border-t border-line py-2.5 pr-2 pl-3">
      <span className="relative shrink-0">
        {avatar}
        {relay}
      </span>
      <div className="min-w-0 flex-1 leading-[1.25] [overflow-wrap:anywhere]">
        <div className="text-[13px] font-semibold text-foreground">{title}</div>
        {detail !== null && (
          <div
            className={cn("text-[11px] text-muted-foreground", problem !== null && "text-bad")}
            role={problem ? "alert" : undefined}
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
