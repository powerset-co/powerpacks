import { useRef } from "react";

import { Kbd } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { usePresence } from "@/hooks/usePresence";
import type { TagAction } from "@/lib/people/facets";

import "./styles/overlays.css";

const PILL = "min-h-[30px] rounded-full";

interface BulkBarProps {
  count: number;
  saving: boolean;
  onAction: (action: TagAction) => void;
  onClear: () => void;
}

// Only while people are selected; it rises in and drops out, keeping its count as it leaves.
export function BulkBar({ count, saving, onAction, onClear }: BulkBarProps) {
  const { mounted, open, onTransitionEnd } = usePresence(count > 0);
  const lastCount = useRef(count);
  if (count) lastCount.current = count;
  if (!mounted) return null;

  return (
    <div
      className="bulkbar rise"
      data-bulkbar
      data-open={open}
      role="toolbar"
      aria-label="Selection"
      aria-hidden={open ? undefined : true}
      onTransitionEnd={onTransitionEnd}
    >
      <b>{lastCount.current.toLocaleString()} selected</b>
      <Button variant="ok" className={PILL} disabled={saving} onClick={() => onAction("share")}>
        Share <Kbd className="ml-0.5">S</Kbd>
      </Button>
      <Button className={PILL} disabled={saving} onClick={() => onAction("private")}>
        Keep private <Kbd className="ml-0.5">P</Kbd>
      </Button>
      <Button variant="ghost" className={PILL} disabled={saving} title="Removes your choice; worth and flags decide" onClick={() => onAction("worth")}>
        Use worth <Kbd className="ml-0.5">W</Kbd>
      </Button>
      <span className="sep" />
      <Button variant="ghost" className={PILL} onClick={onClear}>
        Clear selection <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </div>
  );
}
