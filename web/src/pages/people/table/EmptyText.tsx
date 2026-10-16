import { Button } from "@/components/ui/button"
import type { Tab } from "@/lib/people/view"

const NOBODY: Record<Tab, string> = {
  all: "No people yet.",
  yes: "No one kept yet.",
  maybe: "No one on the fence.",
  no: "No one skipped yet.",
  unjudged: "Everyone has a decision.",
}

interface EmptyTextProps {
  hasPeople: boolean
  filtered: boolean
  tab: Tab
  onClear: () => void
}

export function EmptyText({ hasPeople, filtered, tab, onClear }: EmptyTextProps) {
  if (!hasPeople) return <>No people yet. Ask your agent to build your network.</>
  if (!filtered) return <>{NOBODY[tab]}</>
  return (
    <>
      No people match these filters.{" "}
      <Button variant="ghost" data-clear-filters onClick={onClear}>
        Clear filters
      </Button>
    </>
  )
}
