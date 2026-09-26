import { Button } from "@/components/ui/button";
import type { TagAction } from "@/lib/people/facets";
import type { Person } from "@/types/people";

interface DrawerActionsProps {
  row: Person;
  saving: boolean;
  // Saving, or the drawer is switching away from this person.
  disabled: boolean;
  onAction: (action: TagAction) => void;
}

const PRESSED = "flex-1 min-h-9 aria-pressed:shadow-[inset_0_0_0_1px_var(--line-strong)]";

// Share and Keep private reflect the owner's own tags; Use worth removes that choice.
export function DrawerActions({ row, saving, disabled, onAction }: DrawerActionsProps) {
  const shares = row.tags.includes("share");
  const keepsPrivate = row.tags.includes("private");
  const chosen = shares || keepsPrivate;
  return (
    <>
      <div className="drawer-actions" data-saving={saving || undefined}>
        <Button variant={shares ? "ok" : "default"} className={shares ? "flex-1 min-h-9" : PRESSED} data-one="share" aria-pressed={shares} disabled={disabled} onClick={() => onAction("share")}>
          {shares ? "✓ " : ""}Share
        </Button>
        <Button className={PRESSED} data-one="private" aria-pressed={keepsPrivate} disabled={disabled} onClick={() => onAction("private")}>
          {keepsPrivate ? "✓ " : ""}Keep private
        </Button>
      </div>
      <div className="drawer-worth">
        <Button variant="ghost" className="min-h-7 px-2" data-one="worth" aria-pressed={!chosen} disabled={disabled || !chosen} onClick={() => onAction("worth")}>
          Use worth
        </Button>
        <span>{saving ? "Saving…" : "Removes your choice; worth and flags decide."}</span>
      </div>
    </>
  );
}
