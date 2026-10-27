import parallelLogo from "@/assets/providers/parallel.svg?inline"
import modalLogo from "@/assets/providers/modal.svg?inline"
import openaiLogo from "@/assets/providers/openai.png?inline"
import typesafeLogo from "@/assets/providers/typesafe.png?inline"
import rapidapiLogo from "@/assets/providers/rapidapi.png?inline"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { useState, type FormEvent } from "react"

import { ConnectionRow } from "@/components/shared/ConnectionRow"
import { openExternal } from "@/lib/api/install"
import { isDesktop } from "@/lib/desktop"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchProviders, saveProviderKeys, type Provider } from "@/lib/api/providers"
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
          Services for local processing and search. Keys are provided through Powerset.
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
  "rapidapi-linkedin": rapidapiLogo,
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
      title={provider.id === "modal" ? `${provider.name} (optional)` : provider.name}
      why={provider.description}
      state={provider.connected ? "ready" : "needed"}
      action={{ label: "Connect", onClick: visit }}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {provider.credentials.map((credential) => (
          <span key={credential.name} className="break-all font-mono">
            {credential.name} <span className="text-foreground">{credential.masked ?? "Not set"}</span>
          </span>
        ))}
        <KeysDialog provider={provider} />
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </ConnectionRow>
  )
}

const LINK =
  "cursor-pointer border-0 bg-transparent p-0 text-xs text-primary hover:underline disabled:opacity-50"

/** The provider's keys, typed in: one field per credential, written to .env on Save. */
function KeysDialog({ provider }: { provider: Provider }) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const save = useMutation({
    mutationFn: saveProviderKeys,
    onSuccess: (data) => {
      client.setQueryData(["settings", "providers"], data)
      setValues({})
      setOpen(false)
    },
  })
  const typed = Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim()))
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (Object.keys(typed).length) save.mutate(typed)
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button type="button" className={LINK}>
          {provider.connected ? "Change key" : "Enter key"}
        </button>
      </DialogTrigger>
      <DialogContent>
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{provider.name} key</DialogTitle>
            <DialogDescription>
              Saved to this computer only. A field left empty keeps its current value.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-3">
            {provider.credentials.map((credential) => (
              <label key={credential.name} className="flex flex-col gap-1 text-xs">
                <span className="font-mono text-muted-foreground">{credential.name}</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={values[credential.name] ?? ""}
                  placeholder={credential.masked ?? "Paste the key"}
                  onChange={(event) => setValues({ ...values, [credential.name]: event.target.value })}
                  className="h-8 rounded-[var(--radius-s)] border border-line-strong bg-card px-2 text-sm text-foreground outline-none focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"
                />
              </label>
            ))}
            {save.error ? (
              <p role="alert" className="m-0 text-xs text-bad">
                {errorText(save.error)}
              </p>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="secondary" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={!Object.keys(typed).length || save.isPending}
            >
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
