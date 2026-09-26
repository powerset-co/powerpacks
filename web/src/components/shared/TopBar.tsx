import { NavTabs, type NavTab } from "./NavTabs";

interface TopBarProps {
  brandHref: string;
  tabs: readonly NavTab[];
}

// results.css .topbar/.brand: the brand cell is the rail's width and its right
// border continues the rail line; the tabs start at the main pane.
export function TopBar({ brandHref, tabs }: TopBarProps) {
  return (
    <header className="sticky top-0 z-50 grid h-topbar grid-cols-[228px_minmax(0,1fr)] items-center border-b border-line bg-[color-mix(in_srgb,var(--background)_88%,transparent)] px-5 backdrop-blur-[10px] backdrop-saturate-[1.4] max-[680px]:grid-cols-[auto_minmax(0,1fr)] max-[680px]:px-3.5">
      <a
        href={brandHref}
        className="inline-flex items-center gap-2.5 self-stretch justify-self-stretch border-r border-line text-[11px] font-extrabold tracking-[.16em] text-foreground no-underline before:size-2.5 before:rounded-[3px] before:bg-primary before:shadow-[0_0_0_3px_var(--primary-soft)] before:content-[''] max-[680px]:pr-3.5"
      >
        POWERPACKS
      </a>
      <NavTabs tabs={tabs} />
    </header>
  );
}
