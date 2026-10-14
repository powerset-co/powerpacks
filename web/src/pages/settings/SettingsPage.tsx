import { EmptyState, Page } from "@/components/shared"

/** What someone sets for themselves, from the account menu: nothing yet. Sets stays a local
 *  feature in the debug menu until it ships. */
export function SettingsPage() {
  return (
    <Page title="Settings">
      <EmptyState>Nothing to set yet.</EmptyState>
    </Page>
  )
}
