import { useVirtualizer, type ScrollToOptions } from "@tanstack/react-virtual"
import {
  useCallback,
  useImperativeHandle,
  useRef,
  type HTMLAttributes,
  type ReactNode,
  type Ref,
} from "react"

import { must } from "@/lib/must"

const DEFAULT_OVERSCAN = 24

export interface VirtualRowsHandle {
  element: HTMLDivElement | null
  scrollToIndex: (index: number, options?: ScrollToOptions) => void
  scrollToOffset: (offset: number, options?: ScrollToOptions) => void
}

export interface VirtualRowsProps<T> extends Omit<HTMLAttributes<HTMLDivElement>, "children"> {
  items: readonly T[]
  // Every row's height.
  rowHeight: number
  overscan?: number
  getKey: (item: T, index: number) => string
  renderRow: (item: T, index: number) => ReactNode
  // The owner's scroll control.
  handle?: Ref<VirtualRowsHandle>
}

/**
 * The scroll element is the viewport; rows are absolutely positioned inside a spacer as
 * tall as the whole list. Only the window plus overscan is mounted. The first render
 * already mounts a window's worth (the viewport is assumed as tall as the browser window
 * until measured), so an owner's layout effect finds the rows in the same commit.
 */
export function VirtualRows<T>({
  items,
  rowHeight,
  overscan = DEFAULT_OVERSCAN,
  getKey,
  renderRow,
  handle,
  ...viewport
}: VirtualRowsProps<T>) {
  const scrollRef = useRef<HTMLDivElement>(null)
  // Stable callbacks: the virtualizer re-measures every row whenever getItemKey changes identity.
  const estimateSize = useCallback(() => rowHeight, [rowHeight])
  const getItemKey = useCallback((index: number) => getKey(must(items[index]), index), [items, getKey])
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize,
    overscan,
    getItemKey,
    initialRect: { width: 0, height: window.innerHeight },
  })

  useImperativeHandle(
    handle,
    () => ({
      get element() {
        return scrollRef.current
      },
      scrollToIndex: (index, options) => {
        virtualizer.scrollToIndex(index, options)
      },
      scrollToOffset: (offset, options) => {
        virtualizer.scrollToOffset(offset, options)
      },
    }),
    [virtualizer],
  )

  return (
    <div ref={scrollRef} {...viewport}>
      <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((row) => (
          <div
            key={row.key}
            className="absolute inset-x-0 top-0"
            style={{ height: rowHeight, transform: `translateY(${row.start}px)` }}
          >
            {renderRow(must(items[row.index]), row.index)}
          </div>
        ))}
      </div>
    </div>
  )
}
