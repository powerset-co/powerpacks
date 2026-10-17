import { useNavigate } from "react-router-dom"

import { EmptyState, Page } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"
import { useFeatureSettings, useUpdateFeatures } from "@/lib/features"
import { HOME } from "@/lib/nav"
import { Connect } from "@/pages/install/Connect"

/** What someone sets for themselves, from the account menu. In the desktop app: Repair setup, the
 *  Connect list setup ends on, for an account that never got linked or stopped working. Sets stays
 *  a local feature in the debug menu until it ships. */
export function SettingsPage() {
  const navigate = useNavigate()
  const features = useFeatureSettings()
  const update = useUpdateFeatures()
  if (!isDesktop()) {
    return (
      <Page title="Settings">
        <EmptyState>Nothing to set yet.</EmptyState>
      </Page>
    )
  }
  return (
    <Page title="Settings">
      <section className="flex flex-col gap-3" aria-labelledby="repair-setup">
        <div className="flex flex-col gap-1">
          <h2 id="repair-setup" className="m-0 text-[15px] font-semibold">
            Repair setup
          </h2>
          <p className="m-0 text-sm text-muted-foreground">
            Connect anything that isn't connected: Codex, LinkedIn, Gmail or WhatsApp.
          </p>
        </div>
        <Connect onDone={() => void navigate(HOME.href)} />
      </section>
      <section className="flex flex-col gap-3" aria-labelledby="remote-assist">
        <div className="flex flex-col gap-1">
          <h2 id="remote-assist" className="m-0 text-[15px] font-semibold">
            Remote assist
          </h2>
          <p className="m-0 text-sm text-muted-foreground">
            With this on, someone on the Powerset team can ask your agent to look into a problem with this
            install: it reads your logs, runs commands in your Powerpacks checkout, fixes what it can, and can
            restart the app. Nothing of yours leaves this computer; they get its report.
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
    </Page>
  )
}
