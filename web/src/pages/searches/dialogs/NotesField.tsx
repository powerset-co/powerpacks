import { forwardRef, type KeyboardEvent } from "react"

// server.py _save_feedback rejects a longer comment.
const MAX_COMMENT = 4000

interface NotesFieldProps {
  label: string
  optional: boolean
  placeholder: string
  value: string
  onChange: (value: string) => void
  /** ⌘/Ctrl + Enter; a plain Enter is a new line. */
  onSave: () => void
}

function isSaveKey(event: KeyboardEvent): boolean {
  return event.key === "Enter" && (event.metaKey || event.ctrlKey)
}

export const NotesField = forwardRef<HTMLTextAreaElement, NotesFieldProps>(
  ({ label, optional, placeholder, value, onChange, onSave }, ref) => (
    <label className="block text-sm font-medium">
      {label} {optional ? <span className="font-normal text-muted-foreground">(optional)</span> : null}
      <textarea
        ref={ref}
        name="notes"
        rows={4}
        maxLength={MAX_COMMENT}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (!isSaveKey(event)) return
          event.preventDefault()
          onSave()
        }}
        className="mt-2 block min-h-[100px] w-full resize-y rounded-[6px] border border-border bg-background px-3 py-2.5 text-sm leading-normal text-foreground transition-[border-color] duration-fast ease-out placeholder:text-muted-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-muted-foreground"
      />
    </label>
  ),
)
NotesField.displayName = "NotesField"
