import { BookIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { LOGBOOK } from "@/lib/people/copy"
import type { Person } from "@/types/people"

interface LogbookButtonProps {
  row: Person
  // Saving, or the drawer is switching away from this person.
  disabled: boolean
  // A logbook build is running; one runs at a time.
  building: boolean
  onBuild: () => void
  onView: () => void
}

// The logbook on the drawer's action row: View once this person has a saved one (the reader can
// refresh it), else Build, which saves their raw messages on this computer and changes nothing
// on the row.
export function LogbookButton({ row, disabled, building, onBuild, onView }: LogbookButtonProps) {
  if (row.logbook)
    return (
      <Button
        shape="pill"
        size="sm"
        data-logbook="saved"
        title={LOGBOOK.explainer}
        disabled={disabled}
        onClick={onView}
      >
        <BookIcon className="size-[14px]" />
        {LOGBOOK.view}
      </Button>
    )
  const label = building ? LOGBOOK.building : LOGBOOK.build
  return (
    <Button
      shape="pill"
      size="sm"
      title={LOGBOOK.explainer}
      disabled={building || disabled}
      onClick={onBuild}
    >
      {building ? <Spinner /> : <BookIcon className="size-[14px]" />}
      {label}
    </Button>
  )
}
