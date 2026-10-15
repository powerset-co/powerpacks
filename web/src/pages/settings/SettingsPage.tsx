import { useNavigate } from "react-router-dom"

import { EmptyState, Page } from "@/components/shared"
import { isDesktop } from "@/lib/desktop"
import { HOME } from "@/lib/nav"
import { Connect } from "@/pages/install/Connect"

/** What someone sets for themselves, from the account menu. In the desktop app: Repair setup, the
 *  Connect list setup ends on, for an account that never got linked or stopped working. Sets stays
 *  a local feature in the debug menu until it ships. */
export function SettingsPage() {
  const navigate = useNavigate()
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
    </Page>
  )
}
