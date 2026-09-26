export interface NavTab {
  label: string;
  href: string;
  current: boolean;
}

interface NavTabsProps {
  tabs: readonly NavTab[];
}

// results.css .topbar-nav: page tabs that start where the main pane starts.
export function NavTabs({ tabs }: NavTabsProps) {
  return (
    <nav aria-label="Local UI" className="ml-2.5 inline-flex gap-0.5 justify-self-start">
      {tabs.map((tab) => (
        <a
          key={tab.href}
          href={tab.href}
          aria-current={tab.current ? "page" : undefined}
          className="rounded-sm px-2.5 py-1.5 text-xs font-semibold text-muted-foreground no-underline transition-[color,background-color] duration-fast ease-out hover:bg-secondary hover:text-foreground aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground"
        >
          {tab.label}
        </a>
      ))}
    </nav>
  );
}
