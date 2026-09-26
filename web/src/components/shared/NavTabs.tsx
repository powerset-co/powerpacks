import { PAGES, type PageKey } from "@/lib/nav";

interface NavTabsProps {
  current: PageKey;
}

// results.css .topbar-nav: page tabs that start where the main pane starts.
export function NavTabs({ current }: NavTabsProps) {
  return (
    <nav aria-label="Local UI" className="ml-2.5 inline-flex gap-0.5 justify-self-start">
      {Object.entries(PAGES).map(([key, page]) => (
        <a
          key={key}
          href={page.href}
          aria-current={key === current ? "page" : undefined}
          className="rounded-sm px-2.5 py-1.5 text-xs font-semibold text-muted-foreground no-underline transition-[color,background-color] duration-fast ease-out hover:bg-secondary hover:text-foreground aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground"
        >
          {page.label}
        </a>
      ))}
    </nav>
  );
}
