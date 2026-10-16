import {
  Select as Root,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

export interface SelectOption {
  value: string
  label: string
}

export interface SelectOptionGroup {
  label: string
  options: readonly SelectOption[]
}

interface SelectProps {
  value: string
  onChange: (value: string) => void
  options: readonly (SelectOption | SelectOptionGroup)[]
  /** Shown while nothing is picked (the value is "" and no option is ""): a prompt like "Add…". */
  placeholder?: string
  disabled?: boolean
  className?: string
  "aria-label": string
  title?: string
  [attribute: `data-${string}`]: string | undefined
}

// Radix refuses an item whose value is "", so an empty choice ("any", "All relationships") rides
// under this value and comes back as "".
const EMPTY = "\u0000"

function item({ value, label }: SelectOption) {
  return (
    <SelectItem key={value} value={value === "" ? EMPTY : value}>
      {label}
    </SelectItem>
  )
}

/** The app's one select: the ui Select (Radix) in the field style, options as data. */
export function Select({
  value,
  onChange,
  options,
  placeholder,
  disabled,
  className,
  title,
  ...rest
}: SelectProps) {
  const hasEmptyOption = options.some((entry) =>
    "options" in entry ? entry.options.some((option) => option.value === "") : entry.value === "",
  )
  return (
    <Root
      value={value === "" && hasEmptyOption ? EMPTY : value}
      onValueChange={(next) => onChange(next === EMPTY ? "" : next)}
      disabled={disabled}
    >
      <SelectTrigger
        title={title}
        className={cn(
          "h-8 w-auto gap-2 rounded-[var(--radius-s)] border-line-strong bg-background pl-2.5 pr-2 text-[13px] text-foreground disabled:text-muted-foreground",
          className,
        )}
        {...rest}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent align="start">
        {options.map((entry) =>
          "options" in entry ? (
            <SelectGroup key={entry.label}>
              <SelectLabel>{entry.label}</SelectLabel>
              {entry.options.map(item)}
            </SelectGroup>
          ) : (
            item(entry)
          ),
        )}
      </SelectContent>
    </Root>
  )
}
