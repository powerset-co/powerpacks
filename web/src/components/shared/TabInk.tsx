import { useLayoutEffect, useRef } from "react";

interface TabInkProps {
  // Selects the active tab among the ink's siblings; the parent must be position: relative.
  active: string;
}

// The one sliding underline: a 1px bar moved and stretched by transform, never width or left.
// It jumps into place on first paint and slides (200ms) after; it follows the tab's size.
export function TabInk({ active }: TabInkProps) {
  const ink = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const bar = ink.current!;
    const tab = bar.parentElement!.querySelector<HTMLElement>(active)!;
    const place = () => {
      bar.style.transform = `translateX(${tab.offsetLeft}px) scaleX(${tab.offsetWidth})`;
    };
    place();
    if (!bar.dataset.placed) {
      bar.getBoundingClientRect();
      bar.dataset.placed = "true";
    }
    const observer = new ResizeObserver(place);
    observer.observe(tab);
    return () => observer.disconnect();
  }, [active]);

  return (
    <i
      ref={ink}
      aria-hidden="true"
      className="absolute bottom-0 left-0 h-0.5 w-px origin-left bg-primary data-[placed]:transition-transform data-[placed]:duration-med data-[placed]:ease-out"
    />
  );
}
