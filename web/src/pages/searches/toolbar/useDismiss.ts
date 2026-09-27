import { useEffect, type RefObject } from "react"

export type DismissBy = "escape" | "outside"

/** While `open`: Escape, or a press outside `panel` and `anchor`, calls `onClose`. */
export function useDismiss(
  open: boolean,
  panel: RefObject<HTMLElement | null>,
  anchor: RefObject<HTMLElement | null>,
  onClose: (by: DismissBy) => void,
) {
  useEffect(() => {
    if (!open) return
    const onPointer = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && (panel.current?.contains(target) || anchor.current?.contains(target)))
        return
      onClose("outside")
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose("escape")
    }
    document.addEventListener("pointerdown", onPointer)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("pointerdown", onPointer)
      document.removeEventListener("keydown", onKey)
    }
  }, [open, panel, anchor, onClose])
}
