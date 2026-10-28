import { Page } from "@/components/shared"

import { AccountsSettings } from "./AccountsSettings"

/** Accounts: the connected sources and sign-ins, a page of its own in the side nav. */
export function AccountsPage() {
  return (
    <Page title="Accounts">
      <AccountsSettings />
    </Page>
  )
}
