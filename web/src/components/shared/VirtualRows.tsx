import { useVirtualizer, type ScrollToOptions } from "@tanstack/react-virtual";
import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useRef,
  type ForwardedRef,
  type HTMLAttributes,
  type ReactElement,
  type ReactNode,
  type Ref,
} from "react";

const DEFAULT_OVERSCAN = 24;

export interface VirtualRowsHandle {
  element: HTMLDivElement | null;
  scrollToIndex: (index: number, options?: ScrollToOptions) => void;
  scrollToOffset: (offset: number, options?: ScrollToOptions) => void;
}

export interface VirtualRowsProps<T> extends Omit<HTMLAttributes<HTMLDivElement>, "children"> {
  items: readonly T[];
  rowHeight: number;
  overscan?: number;
  getKey: (item: T, index: number) => string;
  renderRow: (item: T, index: number) => ReactNode;
}

// The scroll element is the viewport; rows are absolutely positioned inside a
// spacer as tall as the whole list. Only the window plus overscan is mounted.
function VirtualRowsInner<T>(
  { items, rowHeight, overscan = DEFAULT_OVERSCAN, getKey, renderRow, ...viewport }: VirtualRowsProps<T>,
  ref: ForwardedRef<VirtualRowsHandle>,
) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Stable callbacks: the virtualizer re-measures every row whenever getItemKey changes identity.
  const estimateSize = useCallback(() => rowHeight, [rowHeight]);
  const getItemKey = useCallback((index: number) => getKey(items[index]!, index), [items, getKey]);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize,
    overscan,
    getItemKey,
  });

  useImperativeHandle(ref, () => ({
    get element() {
      return scrollRef.current;
    },
    scrollToIndex: (index, options) => virtualizer.scrollToIndex(index, options),
    scrollToOffset: (offset, options) => virtualizer.scrollToOffset(offset, options),
  }), [virtualizer]);

  return (
    <div ref={scrollRef} {...viewport}>
      <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((row) => (
          <div
            key={row.key}
            className="absolute inset-x-0 top-0"
            style={{ height: rowHeight, transform: `translateY(${row.start}px)` }}
          >
            {renderRow(items[row.index]!, row.index)}
          </div>
        ))}
      </div>
    </div>
  );
}

export const VirtualRows = forwardRef(VirtualRowsInner) as <T>(
  props: VirtualRowsProps<T> & { ref?: Ref<VirtualRowsHandle> },
) => ReactElement;
