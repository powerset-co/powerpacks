import { useState, useEffect, useCallback, useRef } from "react"
import { ChevronUp, ChevronDown } from "lucide-react"
import { Checkbox } from "@/components/ui/checkbox"
import type { EntityItem } from "@/lib/agent/expandedFilters"

/** Scrollable checkbox list with chevron indicators (like Select scroll buttons) */
export function CheckboxScrollList({
  items,
  selectedIds,
  onAdd,
  onRemove,
}: {
  items: EntityItem[]
  selectedIds: Set<string>
  onAdd: (id: string) => void
  onRemove: (id: string) => void
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [canScrollUp, setCanScrollUp] = useState(false)
  const [canScrollDown, setCanScrollDown] = useState(false)

  const checkScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setCanScrollUp(el.scrollTop > 2)
    setCanScrollDown(el.scrollTop + el.clientHeight < el.scrollHeight - 2)
  }, [])

  useEffect(() => {
    checkScroll()
    const el = scrollRef.current
    if (el) el.addEventListener("scroll", checkScroll, { passive: true })
    return () => el?.removeEventListener("scroll", checkScroll)
  }, [checkScroll])

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-center h-5">
        {canScrollUp && <ChevronUp className="h-4 w-4 text-muted-foreground" />}
      </div>
      <div
        ref={scrollRef}
        className="max-h-48 overflow-y-auto px-2"
        onWheel={(e) => {
          // Prevent scroll from bubbling to parent ScrollArea (e.g. Dialog)
          const el = scrollRef.current
          if (!el) return
          const { scrollTop, scrollHeight, clientHeight } = el
          const atTop = scrollTop === 0 && e.deltaY < 0
          const atBottom = scrollTop + clientHeight >= scrollHeight && e.deltaY > 0
          if (!atTop && !atBottom) e.stopPropagation()
        }}
      >
        <div className="space-y-1">
          {items.map((item) => {
            const isChecked = selectedIds.has(item.id)
            return (
              <label
                key={item.id}
                className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-accent transition-colors cursor-pointer text-sm"
              >
                <Checkbox
                  checked={isChecked}
                  onCheckedChange={(checked) => (checked ? onAdd(item.id) : onRemove(item.id))}
                  className="h-3.5 w-3.5"
                />
                {item.display_value}
              </label>
            )
          })}
        </div>
      </div>
      <div className="flex items-center justify-center h-5">
        {canScrollDown && <ChevronDown className="h-4 w-4 text-muted-foreground" />}
      </div>
    </div>
  )
}
