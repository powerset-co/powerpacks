import type { CSSProperties, ReactNode } from "react";

import { TopBar, type NavTab } from "@/components/shared";

import { ROW_H } from "./table/columns";
import "./styles/shell.css";
import "./styles/rail.css";

const ROW_HEIGHT = { "--row-h": `${ROW_H}px` } as CSSProperties;

const NAV: readonly NavTab[] = [
  { label: "Searches", href: "/searches", current: false },
  { label: "People", href: "/people", current: true },
];

interface PeopleShellProps {
  drawerOpen: boolean;
  rail: ReactNode;
  main: ReactNode;
  overlays?: ReactNode;
}

// Top bar, then the rail beside the main pane; the drawer, bulk bar and toast float over both.
export function PeopleShell({ drawerOpen, rail, main, overlays }: PeopleShellProps) {
  return (
    <div className="app-shell people-shell" data-people data-drawer-open={String(drawerOpen)} style={ROW_HEIGHT}>
      <TopBar brandHref="/people" tabs={NAV} />
      <div className="people-layout">
        <aside className="rail" data-rail aria-label="Filters">{rail}</aside>
        <main className="people-main">{main}</main>
      </div>
      {overlays}
    </div>
  );
}
