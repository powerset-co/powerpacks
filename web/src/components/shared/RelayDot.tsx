import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { signIn } from "@/lib/api/feedback"
import { connectRelay, fetchRelay, type RelayStatus } from "@/lib/api/relay"

const RELAY_KEY = ["relay"]
const POLL_MS = 5_000

function hoverText(status: RelayStatus | undefined, signingIn: boolean): string {
  if (signingIn) return "Signing in…"
  if (!status) return "Checking Powerset Relay…"
  if (status.state === "connected") return "Connected to Powerset Relay"
  if (status.state === "signed_out") return "Not connected to Powerset Relay · Click to sign in"
  return "Not connected to Powerset Relay · Click to retry"
}

// Green while the Ask the Set daemon holds its relay connection, grey otherwise. Grey is a
// button: sign in (when signed out) and wake the daemon so it reconnects at once.
export function RelayDot() {
  const client = useQueryClient()
  const relay = useQuery({
    queryKey: RELAY_KEY,
    queryFn: ({ signal }) => fetchRelay(signal),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: true,
  })
  const connect = useMutation({
    mutationFn: async () => {
      if (relay.data?.state === "signed_out") await signIn()
      return connectRelay()
    },
    onSettled: () => client.invalidateQueries({ queryKey: RELAY_KEY }),
  })
  const connected = relay.data?.state === "connected"
  const label = hoverText(relay.data, connect.isPending)
  return (
    <button
      type="button"
      aria-label={label}
      aria-disabled={connected || connect.isPending}
      onClick={() => {
        if (!connected && !connect.isPending) connect.mutate()
      }}
      className={`group relative grid size-5 shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 ${connected ? "cursor-default" : "cursor-pointer"}`}
    >
      <span
        className={
          connected
            ? "size-2 rounded-full bg-[var(--ok)] shadow-[0_0_0_3px_var(--ok-soft)]"
            : "size-2 rounded-full bg-[var(--faint)] opacity-70 group-hover:opacity-100"
        }
      />
      <span
        role="tooltip"
        className="pointer-events-none absolute top-[calc(100%+8px)] left-1/2 -translate-x-1/2 rounded-md border border-line bg-[var(--background)] px-2.5 py-1.5 text-[12px] font-medium tracking-normal whitespace-nowrap text-foreground opacity-0 shadow-sm transition-opacity duration-100 group-hover:opacity-100 group-focus-visible:opacity-100"
      >
        {label}
      </span>
    </button>
  )
}
