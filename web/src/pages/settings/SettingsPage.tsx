import { useUpdate } from "@/hooks/useUpdate"
import { Page } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { ACCOUNT_KEY, fetchAccount } from "@/lib/api/account"
import { useFeatureSettings, useUpdateFeatures } from "@/lib/features"
import { useQuery } from "@tanstack/react-query"
import { AccountsSettings } from "./AccountsSettings"
import { ProviderSettings } from "./ProviderSettings"

/** Local API connections, account repair and remote-assist preferences, from the account menu. */
export function SettingsPage() {
  const version = useUpdate()?.current
  const features = useFeatureSettings()
  const update = useUpdateFeatures()
  const account = useQuery({ queryKey: ACCOUNT_KEY, queryFn: ({ signal }) => fetchAccount(signal) })
  const onTeam =
    account.data?.signed_in === true && (account.data.email?.toLowerCase().endsWith("@powerset.co") ?? false)
  return (
    <Page title="Settings">
      <AccountsSettings />
      {onTeam ? <ProviderSettings /> : null}
      <section className="flex flex-col gap-3" aria-labelledby="remote-assist">
        <div className="flex flex-col gap-1">
          <h2 id="remote-assist" className="m-0 text-[15px] font-semibold">
            Remote assist
          </h2>
          <p className="m-0 text-sm text-muted-foreground">
            With this on, someone on the Powerset team can ask your agent to look into a problem with this
            install: it reads your logs, runs commands in your Powerpacks checkout, fixes what it can, and can
            update or restart the app. They get its report, which may include how a person they name is
            labeled and why; nothing else of yours leaves this computer.
          </p>
        </div>
        <label className="flex w-fit cursor-pointer items-center gap-3 text-[13px]">
          <input
            type="checkbox"
            checked={features.data?.support ?? false}
            disabled={!features.data || update.isPending}
            onChange={() => {
              if (features.data) update.mutate({ ...features.data, support: !features.data.support })
            }}
            className="accent-[var(--primary)]"
          />
          Let the Powerset team work on this install
        </label>
        {update.error ? (
          <p role="alert" className="m-0 text-xs text-bad">
            {errorText(update.error)}
          </p>
        ) : null}
      </section>
      {onTeam ? (
        <section className="flex flex-col gap-3" aria-labelledby="team-page">
          <div className="flex flex-col gap-1">
            <h2 id="team-page" className="m-0 text-[15px] font-semibold">
              Team page
            </h2>
            <p className="m-0 text-sm text-muted-foreground">
              Work on a teammate's install from this machine through their agent. Shows Team in the side nav.
            </p>
          </div>
          <label className="flex w-fit cursor-pointer items-center gap-3 text-[13px]">
            <input
              type="checkbox"
              checked={features.data?.team ?? false}
              disabled={!features.data || update.isPending}
              onChange={() => {
                if (features.data) update.mutate({ ...features.data, team: !features.data.team })
              }}
              className="accent-[var(--primary)]"
            />
            Show the Team page
          </label>
        </section>
      ) : null}
      {version ? (
        <p
          title="Powerpacks version"
          className="m-0 border-t border-line pt-4 text-[11px] text-faint tabular-nums"
        >
          Powerpacks v{version}
        </p>
      ) : null}
    </Page>
  )
}
