import parallelLogo from "@/assets/providers/parallel.svg?inline"
import modalLogo from "@/assets/providers/modal.svg?inline"
import openaiLogo from "@/assets/providers/openai.png?inline"
import typesafeLogo from "@/assets/providers/typesafe.png?inline"
import rapidapiLogo from "@/assets/providers/rapidapi.png?inline"

import { useQuery } from "@tanstack/react-query"

import { useState } from "react"

import { ConnectionRow } from "@/components/shared/ConnectionRow"
import { openExternal } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchProviders, type Provider } from "@/lib/api/providers"
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
          Optional API connections
        </h2>
        <p className="m-0 text-sm text-muted-foreground">
          For development. Powerset handles these connections for you.
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

const LOGOS = {
  parallel: parallelLogo,
  modal: modalLogo,
  openai: openaiLogo,
  typesafe: typesafeLogo,
  "rapidapi-twitter": rapidapiLogo,
}

function ProviderRow({ provider }: { provider: Provider }) {
  const [error, setError] = useState<string | null>(null)
  const dashboard = provider.connect_url
  const visit = () => {
    setError(null)
    if (isDesktop()) void openExternal(dashboard).catch((caught: unknown) => setError(errorText(caught)))
    else window.open(dashboard, "_blank", "noopener,noreferrer")
  }
  return (
    <ConnectionRow
      icon={<img src={LOGOS[provider.id]} alt="" className="size-[18px] object-contain" />}
      title={`${provider.name} (optional)`}
      why={provider.description}
      state={provider.connected ? "ready" : "needed"}
      action={{ label: "Connect", onClick: visit }}
    >
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {provider.credentials.map((credential) => (
          <span key={credential.name} className="break-all font-mono">
            {credential.name} <span className="text-foreground">{credential.masked ?? "Not set"}</span>
          </span>
        ))}
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </ConnectionRow>
  )
}
