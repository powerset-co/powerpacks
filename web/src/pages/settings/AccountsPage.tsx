import { useSearchParams } from "react-router-dom"
import { Checklist } from "@/pages/onboarding/Checklist"
import { useRun } from "@/pages/onboarding/run"
import { Page } from "@/components/shared"
import { isDesktop } from "@/lib/desktop"
import { AccountsSettings } from "./AccountsSettings"

function BrowserProgress() {
  const run = useRun(() => true)
  return <Checklist run={run} />
}

export function AccountsPage() {
  const [params] = useSearchParams()
  return isDesktop() ? (
    <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <AccountsSettings panel />
    </main>
  ) : (
    <Page title="Accounts">{params.get("view") === "sync" ? <BrowserProgress /> : <AccountsSettings />}</Page>
  )
}
