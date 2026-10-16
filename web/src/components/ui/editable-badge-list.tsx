import * as React from "react"
import { useState, useRef, useEffect } from "react"
import { X, Check, Plus, Pencil } from "lucide-react"
import { cn } from "@/lib/utils"
import { badgeVariants } from "./badge-variants"
import { type VariantProps } from "class-variance-authority"

export interface EditableBadgeListProps extends VariantProps<typeof badgeVariants> {
  items: string[]
  onAdd?: (value: string) => void
  onRemove?: (value: string, index: number) => void
  onEdit?: (oldValue: string, newValue: string, index: number) => void
  placeholder?: string
  className?: string
}

function InlineInput({
  initialValue,
  placeholder,
  variant,
  onConfirm,
  onCancel,
}: {
  initialValue: string
  placeholder: string
  variant: VariantProps<typeof badgeVariants>["variant"]
  onConfirm: (value: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initialValue)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  const handleConfirm = () => {
    if (value.trim() && value.trim() !== initialValue) {
      onConfirm(value.trim())
    } else {
      onCancel()
    }
  }

  return (
    <div className={cn(badgeVariants({ variant }), "flex items-center gap-1 pr-1")}>
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault()
            handleConfirm()
          } else if (e.key === "Escape") onCancel()
        }}
        onBlur={handleConfirm}
        placeholder={placeholder}
        className="bg-transparent border-none outline-none text-xs min-w-[80px] w-auto placeholder:text-muted-foreground/60"
        style={{ width: `${Math.max(80, value.length * 7)}px` }}
      />
      <button onClick={handleConfirm} className="p-0.5 hover:bg-primary/20 rounded transition-colors">
        <Check className="h-3 w-3 text-primary" />
      </button>
      <button onClick={onCancel} className="p-0.5 hover:bg-destructive/20 rounded transition-colors">
        <X className="h-3 w-3 text-muted-foreground hover:text-destructive" />
      </button>
    </div>
  )
}

function EditableBadgeList({
  items,
  onAdd,
  onRemove,
  onEdit,
  variant = "secondary",
  placeholder = "Type here...",
  className,
}: EditableBadgeListProps) {
  const [isAdding, setIsAdding] = useState(false)
  const [newValue, setNewValue] = useState("")
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (isAdding && inputRef.current) {
      inputRef.current.focus()
    }
  }, [isAdding])

  const handleConfirm = () => {
    if (newValue.trim()) {
      onAdd?.(newValue.trim())
    }
    setNewValue("")
    setIsAdding(false)
  }

  const handleCancel = () => {
    setNewValue("")
    setIsAdding(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault()
      handleConfirm()
    } else if (e.key === "Escape") {
      handleCancel()
    }
  }

  return (
    <div className={cn("flex flex-wrap gap-x-[0.1rem] gap-y-2", className)}>
      {items.map((item, idx) =>
        editingIndex === idx ? (
          <InlineInput
            key={`edit-${idx}`}
            initialValue={item}
            placeholder={placeholder}
            variant={variant}
            onConfirm={(newVal) => {
              onEdit?.(item, newVal, idx)
              setEditingIndex(null)
            }}
            onCancel={() => setEditingIndex(null)}
          />
        ) : (
          <div key={`${item}-${idx}`} className="relative group">
            {/* Spacer — reserves expanded width (label + pencil + X) */}
            <span
              className="inline-flex items-center text-xs font-semibold px-2.5 py-0.5 border border-transparent opacity-0 pointer-events-none"
              aria-hidden
            >
              {item}
              {onEdit && <Pencil className="h-3 w-3 ml-1" />}
              <X className="h-3 w-3 ml-0.5" />
            </span>
            {/* Badge on top — click pencil to edit, X to remove */}
            <div
              className={cn(
                badgeVariants({ variant }),
                "absolute top-0 bottom-0 left-0 z-10 cursor-default overflow-hidden transition-all duration-200",
              )}
            >
              <span className="flex items-center">
                {item}
                {onEdit && (
                  <span
                    className="inline-flex items-center max-w-0 overflow-hidden opacity-0 group-hover:max-w-[20px] group-hover:opacity-100 group-hover:ml-1 transition-all duration-200 cursor-pointer"
                    onClick={(e) => {
                      e.stopPropagation()
                      setEditingIndex(idx)
                    }}
                  >
                    <Pencil className="h-3 w-3 text-muted-foreground hover:text-primary transition-colors" />
                  </span>
                )}
                <span
                  className="inline-flex items-center max-w-0 overflow-hidden opacity-0 group-hover:max-w-[20px] group-hover:opacity-100 group-hover:ml-0.5 transition-all duration-200 cursor-pointer"
                  onClick={(e) => {
                    e.stopPropagation()
                    onRemove?.(item, idx)
                  }}
                >
                  <X className="h-3 w-3 text-muted-foreground hover:text-destructive transition-colors" />
                </span>
              </span>
            </div>
          </div>
        ),
      )}

      {/* New item input (when adding) */}
      {isAdding && (
        <div className={cn(badgeVariants({ variant }), "flex items-center gap-1 pr-1 animate-fade-in")}>
          <input
            ref={inputRef}
            type="text"
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            className="bg-transparent border-none outline-none text-xs w-24 min-w-0 placeholder:text-muted-foreground/60"
          />
          <button onClick={handleConfirm} className="p-0.5 hover:bg-primary/20 rounded transition-colors">
            <Check className="h-3 w-3 text-primary" />
          </button>
          <button onClick={handleCancel} className="p-0.5 hover:bg-destructive/20 rounded transition-colors">
            <X className="h-3 w-3 text-muted-foreground hover:text-destructive" />
          </button>
        </div>
      )}

      {/* Add button */}
      <div
        onClick={() => setIsAdding(true)}
        className={cn(
          badgeVariants({ variant: "outline" }),
          "cursor-pointer hover:bg-accent transition-colors",
          isAdding && "opacity-50 pointer-events-none",
        )}
      >
        <Plus className="h-3 w-3 mr-1" />
        Add
      </div>
    </div>
  )
}

export { EditableBadgeList }
