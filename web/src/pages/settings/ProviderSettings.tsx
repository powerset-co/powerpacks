import { useQuery } from "@tanstack/react-query"

import { useState } from "react"
import { Cloud, Database, Globe, ShieldCheck, Sparkles } from "lucide-react"

import { Fold, PowersetMark, CHANNEL_ICON } from "@/components/shared"
import { ConnectionRow } from "@/components/shared/ConnectionRow"
import { openExternal } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchProviders, fetchProviderUsage, type Provider } from "@/lib/api/providers"
import { errorText } from "@/lib/api/http"

/** Local key status has its own query so it never holds up accounts or other settings. */
export function ProviderSettings() {
  const providers = useQuery({
    queryKey: ["settings", "providers"],
    queryFn: ({ signal }) => fetchProviders(signal),
  })
  return (
    <section className="flex flex-col gap-3" aria-labelledby="api-connections">
      <div className="flex flex-col gap-1">
        <h2 id="api-connections" className="m-0 text-[15px] font-semibold">
          API connections
        </h2>
        <p className="m-0 text-sm text-muted-foreground">
          Keys on this computer. Usage is shown where the API key supports it.
        </p>
      </div>
      {providers.isPending ? (
        <div aria-label="Loading API connections" aria-busy="true" className="flex flex-col gap-3">
          {[0, 1, 2].map((row) => (
            <div
              key={row}
              className="flex items-center gap-3 rounded-[var(--radius-l)] border border-line bg-card p-4"
            >
              <Skeleton className="h-4 w-28 bg-surface-2" />
              <Skeleton className="ml-auto h-5 w-20 rounded-full bg-surface-2" />
            </div>
          ))}
        </div>
      ) : providers.error ? (
        <div className="flex items-center gap-3">
          <p role="alert" className="m-0 text-xs text-bad">
            {errorText(providers.error)}
          </p>
          <Button size="sm" variant="secondary" onClick={() => void providers.refetch()}>
            Try again
          </Button>
        </div>
      ) : (
        <ul
          className="m-0 flex list-none flex-col divide-y divide-line rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left"
          aria-label="API connections"
        >
          {providers.data.providers.map((provider) => (
            <ProviderRow key={provider.id} provider={provider} />
          ))}
        </ul>
      )}
    </section>
  )
}

const ICONS = {
  parallel: Globe,
  modal: Cloud,
  powerset: PowersetMark,
  openai: Sparkles,
  typesafe: ShieldCheck,
  turbopuffer: Database,
  "rapidapi-twitter": CHANNEL_ICON.x,
}

function ProviderRow({ provider }: { provider: Provider }) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const usage = useQuery({
    queryKey: ["settings", "providers", provider.id, "usage"],
    queryFn: ({ signal }) => fetchProviderUsage(provider.id, signal),
    enabled: open && provider.connected && provider.id === "modal",
    staleTime: 60_000,
    retry: false,
  })
  const Icon = ICONS[provider.id]
  const dashboard = provider.connect_url
  const visit = () => {
    setError(null)
    if (isDesktop()) void openExternal(dashboard).catch((caught: unknown) => setError(errorText(caught)))
    else window.open(dashboard, "_blank", "noopener,noreferrer")
  }
  return (
    <ConnectionRow
      icon={<Icon className="size-[18px]" />}
      title={
        provider.id === "modal" && provider.connected ? (
          <button
            type="button"
            className="chevron border-0 bg-transparent p-0 text-left font-bold text-inherit"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {provider.name}
          </button>
        ) : (
          provider.name
        )
      }
      why={provider.description}
      state={provider.connected ? "ready" : "needed"}
      action={{ label: "Connect", onClick: visit }}
    >
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
        {provider.credentials.map((credential) => (
          <span key={credential.name} className="break-all font-mono">
            {credential.name} <span className="text-foreground">{credential.masked ?? "Not set"}</span>
          </span>
        ))}
      </div>
      <Fold open={open}>
        <div className="flex flex-col gap-2 pt-2 text-xs">
          {provider.connected &&
            provider.id === "modal" &&
            (usage.isPending ? (
              <Skeleton aria-label="Loading usage" className="h-4 w-48 bg-surface-2" />
            ) : usage.error ? (
              <p role="alert" className="m-0 text-bad">
                {errorText(usage.error)}
              </p>
            ) : (
              <>
                {usage.data.metrics.map((metric) => (
                  <div key={metric.label} className="flex gap-2">
                    <span className="text-muted-foreground">{metric.label}</span>
                    <span className="tabular-nums">{metric.value}</span>
                  </div>
                ))}
                {usage.data.note && <p className="m-0 text-muted-foreground">{usage.data.note}</p>}
              </>
            ))}
        </div>
      </Fold>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </ConnectionRow>
  )
}
