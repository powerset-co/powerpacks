import { useEffect, useRef } from "react";

import { Kbd } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { usePresence } from "@/hooks/usePresence";
import { USE_WORTH_HELP } from "@/lib/people/copy";
import type { TagAction } from "@/lib/people/facets";

import "./styles/overlays.css";

const BAR_BUTTON = "min-h-[30px]";

interface BulkBarProps {
  count: number;
  saving: boolean;
  onAction: (action: TagAction) => void;
  onClear: () => void;
}

// Only while people are selected; it rises in and drops out, keeping its count as it leaves.
// Inert whenever it is not open, exit included, so a leaving bar takes no focus or clicks.
export function BulkBar({ count, saving, onAction, onClear }: BulkBarProps) {
  const { mounted, open, onTransitionEnd } = usePresence(count > 0);
  const bar = useRef<HTMLDivElement>(null);
  const lastCount = useRef(count);
  if (count) lastCount.current = count;

  useEffect(() => {
    if (bar.current) bar.current.inert = !open;
  }, [open, mounted]);

  if (!mounted) return null;
  return (
    <div
      ref={bar}
      className="bulkbar rise"
      data-bulkbar
      data-open={open}
      role="toolbar"
      aria-label="Selection"
      aria-hidden={open ? undefined : true}
      onTransitionEnd={onTransitionEnd}
    >
      <b>{lastCount.current.toLocaleString()} selected</b>
      <Button variant="ok" shape="pill" className={BAR_BUTTON} disabled={saving} onClick={() => onAction("share")}>
        Share <Kbd className="ml-0.5">S</Kbd>
      </Button>
      <Button shape="pill" className={BAR_BUTTON} disabled={saving} onClick={() => onAction("private")}>
        Keep private <Kbd className="ml-0.5">P</Kbd>
      </Button>
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} disabled={saving} title={USE_WORTH_HELP} onClick={() => onAction("worth")}>
        Use worth <Kbd className="ml-0.5">W</Kbd>
      </Button>
      <span className="sep" />
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={onClear}>
        Clear selection <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </div>
  );
}
