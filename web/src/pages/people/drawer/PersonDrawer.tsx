import { useCallback, useEffect, useRef, useState } from "react";

import type { DetailState } from "@/hooks/usePersonDetail";
import type { TagAction } from "@/lib/people/facets";
import { toggled } from "@/lib/sets";
import type { Person } from "@/types/people";

import "../styles/drawer.css";
import "../styles/overlays.css";
import { DecisionSection } from "./DecisionSection";
import { DetailSections } from "./DetailSections";
import { DetailFailed, DetailLoading } from "./DetailStatus";
import { DrawerActions } from "./DrawerActions";
import { DrawerHeader } from "./DrawerHeader";
import { OPEN_BY_DEFAULT, type SectionKey, type SectionState } from "./sections";
import { useDrawerSwap } from "./useDrawerSwap";

interface PersonDrawerProps {
  // The person shown; stays set while the drawer animates shut.
  row: Person | null;
  open: boolean;
  detail: DetailState;
  saving: boolean;
  onAction: (action: TagAction) => void;
  onClose: () => void;
  onRetry: () => void;
}

// A fixed, non-modal panel over the right edge. Which sections are open carries across people.
export function PersonDrawer(props: PersonDrawerProps) {
  const { open, saving, onAction, onClose, onRetry } = props;
  const panel = useRef<HTMLElement>(null);
  const [sections, setSections] = useState<ReadonlySet<SectionKey>>(OPEN_BY_DEFAULT);
  const { row, detail, leaving, swapping, onTransitionEnd } = useDrawerSwap(props.row, props.detail, open);
  const id = row?.parent_id ?? null;

  useEffect(() => {
    if (panel.current) panel.current.scrollTop = 0;
  }, [id]);

  useEffect(() => {
    if (panel.current) panel.current.inert = !open;
  }, [open]);

  const section: SectionState = useCallback((key) => ({
    open: sections.has(key),
    onToggle: (next) => setSections((current) => toggled(current, key, next)),
  }), [sections]);

  const ready = detail.status === "ready" ? detail.detail : null;
  return (
    <aside ref={panel} className="drawer" data-drawer aria-hidden={open ? undefined : true} aria-label="Person">
      {row ? (
        <div
          key={row.parent_id}
          className={swapping && !leaving ? "drawer-inner swap" : "drawer-inner"}
          data-leaving={leaving || undefined}
          onTransitionEnd={onTransitionEnd}
        >
          <DrawerHeader row={row} detail={ready} onClose={onClose} />
          <DrawerActions row={row} saving={saving} disabled={saving || leaving} onAction={onAction} />
          <DecisionSection row={row} detail={ready} {...section("decision")} />
          {detail.status === "loading" ? <DetailLoading /> : null}
          {detail.status === "failed" ? <DetailFailed onRetry={onRetry} /> : null}
          {ready ? <DetailSections row={row} detail={ready} section={section} /> : null}
        </div>
      ) : null}
    </aside>
  );
}
