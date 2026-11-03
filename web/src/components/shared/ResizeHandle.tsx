import { useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react"

import { cn } from "@/lib/utils"

interface ResizeHandleProps {
  label: string
  /** Which edge of its (relatively positioned) panel the handle sits on. */
  edge: "left" | "right"
  /** Called on every move with the pointer's x; the caller turns it into a size. */
  onDrag: (clientX: number) => void
  /** An arrow key: grow (+1) or shrink (-1) the panel by a step. */
  onStep: (direction: 1 | -1) => void
  /** Double-click: back to the default size. */
  onReset: () => void
  /** Placement beyond the edge's, for a panel that scrolls (the drawer pins its handle). */
  style?: CSSProperties
}

/**
 * The strip on a panel's edge that resizes it: drag it, use the arrow keys on it, or double-click
 * it for the default size. A thin line shows on hover and while dragging. While dragging, the page
 * keeps the resize cursor and selects no text.
 */
export function ResizeHandle({ label, edge, onDrag, onStep, onReset, style }: ResizeHandleProps) {
  const dragging = useRef(false)
  const [active, setActive] = useState(false)

  const end = (event: PointerEvent<HTMLButtonElement>) => {
    if (!dragging.current) return
    dragging.current = false
    setActive(false)
    document.documentElement.removeAttribute("data-resizing")
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId)
  }

  return (
    // A button, so it is a tab stop; the arrow keys step it.
    <button
      type="button"
      aria-label={label}
      title="Drag to resize · double-click to reset"
      data-active={active || undefined}
      style={style}
      className={cn(
        "group absolute inset-y-0 z-30 w-2 cursor-col-resize touch-none border-0 bg-transparent p-0 outline-none",
        edge === "right" ? "-right-1" : "-left-1",
      )}
      onPointerDown={(event) => {
        if (event.button !== 0) return
        event.preventDefault()
        event.currentTarget.setPointerCapture(event.pointerId)
        dragging.current = true
        setActive(true)
        document.documentElement.setAttribute("data-resizing", "")
      }}
      onPointerMove={(event) => {
        if (dragging.current) onDrag(event.clientX)
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={onReset}
      onKeyDown={(event: KeyboardEvent<HTMLButtonElement>) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
        event.preventDefault()
        // The panel grows toward its handle's side.
        const outward = (event.key === "ArrowRight") === (edge === "right")
        onStep(outward ? 1 : -1)
      }}
    >
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-primary opacity-0 transition-opacity delay-100 duration-fast group-hover:opacity-60 group-focus-visible:opacity-100 group-data-[active]:opacity-100 group-data-[active]:delay-0"
      />
    </button>
  )
}
