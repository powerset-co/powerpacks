import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react"
import { createPortal } from "react-dom"

import { CLOSE_MARK, PlusIcon } from "@/components/shared"
import { type DismissBy, useDismiss } from "@/hooks/useDismiss"
import { usePresence } from "@/hooks/usePresence"
import { usePresenceList } from "@/hooks/usePresenceList"
import { existingTag, normalizeTag, TAG_NAME_MAX } from "@/lib/searches/tags"
import { cn } from "@/lib/utils"

const PANEL_WIDTH = 256
const PANEL_GAP = 6
const EDGE = 8

export interface TagEditorProps {
  personName: string
  // Every tag in the search, and the ones this person holds.
  tags: readonly string[]
  applied: readonly string[]
  // While the saved tags load: an edit then would overwrite them.
  disabled: boolean
  onToggle: (tag: string) => void
  onRemove: (tag: string) => void
}

// Below the trigger, kept inside the window. Fixed and portalled: a virtual row clips and
// recycles its cells, so the panel lives on the body and closes once a scroll moves its row.
function panelTop(anchor: HTMLElement): number {
  return anchor.getBoundingClientRect().bottom + PANEL_GAP
}

function placeBelow(anchor: HTMLElement): CSSProperties {
  const box = anchor.getBoundingClientRect()
  return {
    top: panelTop(anchor),
    left: Math.max(EDGE, Math.min(box.left, window.innerWidth - PANEL_WIDTH - EDGE)),
    width: PANEL_WIDTH,
  }
}

const sameTag = (tag: string) => tag

// The row's "+ tag" button (its tags as chips) and the popover that edits them: tags as
// toggles, a field that adds (Enter), × to delete a tag from the search. Escape closes.
// A chip taken off leaves with the .rise exit; one added rises in.
export function TagEditor({ personName, tags, applied, disabled, onToggle, onRemove }: TagEditorProps) {
  const [place, setPlace] = useState<CSSProperties | null>(null)
  const [text, setText] = useState("")
  const anchor = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)
  // What was on screen already (the row's chips at mount, the list at open) arrives with its
  // container; only a tag added afterwards rises in on its own.
  const [chipsAtMount] = useState(() => new Set(applied))
  const [tagsAtOpen, setTagsAtOpen] = useState<ReadonlySet<string>>(() => new Set(tags))
  const presence = usePresence(place)
  const chips = usePresenceList(applied, sameTag)
  const open = place !== null

  const close = useCallback((by: DismissBy) => {
    setPlace(null)
    setText("")
    if (by === "escape") anchor.current?.focus()
  }, [])
  useDismiss(open, panel, anchor, close)

  useEffect(() => {
    if (!place) return
    input.current?.focus()
    // A scroll that leaves the trigger where it was (a clamp, a re-measure) keeps it open.
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && panel.current?.contains(event.target)) return
      if (anchor.current && panelTop(anchor.current) === place.top) return
      close("outside")
    }
    window.addEventListener("scroll", onScroll, true)
    return () => window.removeEventListener("scroll", onScroll, true)
  }, [place, close])

  const typed = normalizeTag(text)
  const matching = typed ? tags.filter((tag) => tag.toLowerCase().includes(typed.toLowerCase())) : tags
  const canCreate = typed !== "" && !existingTag(tags, typed)
  const held = new Set(applied)

  const apply = (tag: string) => {
    onToggle(tag)
    setText("")
  }
  const onEnter = () => {
    const only = matching.length === 1 ? matching[0] : undefined
    if (canCreate) apply(typed)
    else if (only !== undefined) apply(only)
  }

  return (
    <>
      <button
        ref={anchor}
        type="button"
        data-row-action="tag"
        disabled={disabled}
        aria-label={`${applied.length ? "Edit tags for" : "Add tag to"} ${personName}`}
        title={applied.length ? "Edit tags" : "Add tag"}
        aria-expanded={open}
        className={cn(
          // results.css .tag-trigger: shown on the row's hover (the row carries `group/row`),
          // on focus, while open, or once the person holds a tag.
          "inline-flex min-h-7 min-w-7 max-w-full cursor-pointer items-center justify-end gap-1 overflow-hidden rounded-full px-[7px] py-[3px] text-muted-foreground opacity-0 transition-[color,background-color,opacity] duration-fast ease-out hover:bg-secondary hover:text-foreground focus-visible:opacity-100 disabled:cursor-default group-hover/row:opacity-100",
          (applied.length > 0 || open) && "opacity-100",
        )}
        onClick={() => {
          if (open) close("outside")
          else if (anchor.current) {
            setTagsAtOpen(new Set(tags))
            setPlace(placeBelow(anchor.current))
          }
        }}
      >
        {chips.map((chip) => (
          <span
            key={chip.key}
            title={chip.item}
            data-tag={chip.item}
            data-open={chip.open}
            onTransitionEnd={chip.onTransitionEnd}
            className={cn(
              "rise h-5 min-w-0 max-w-[100px] truncate rounded-full bg-secondary px-[7px] py-0.5 text-[10px] font-semibold leading-4 text-foreground",
              chipsAtMount.has(chip.key) && "rise-settled",
            )}
          >
            {chip.item}
          </span>
        ))}
        <PlusIcon className="size-3" />
      </button>
      {presence.mounted && presence.shown
        ? createPortal(
            <div
              ref={panel}
              role="dialog"
              aria-label={`Tags for ${personName}`}
              data-open={presence.open}
              onTransitionEnd={presence.onTransitionEnd}
              className={"float-panel rise fixed"}
              style={presence.shown}
            >
              <input
                ref={input}
                type="text"
                value={text}
                maxLength={TAG_NAME_MAX}
                placeholder="Add tag…"
                aria-label="Add tag"
                className="h-8 rounded-sm border border-input bg-background px-2 text-xs text-foreground placeholder:text-faint"
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                  // The page's own shortcuts must not see typing.
                  event.stopPropagation()
                  if (event.key === "Escape") close("escape")
                  if (event.key !== "Enter") return
                  event.preventDefault()
                  onEnter()
                }}
              />
              <div className="grid max-h-56 gap-0.5 overflow-y-auto">
                {!matching.length && !canCreate ? (
                  <p className="px-1.5 py-1 text-muted-foreground">
                    {tags.length ? "No matching tags" : "No tags yet. Type to create one."}
                  </p>
                ) : null}
                {matching.map((tag) => (
                  <span
                    key={tag}
                    className={cn("flex items-center gap-1", !tagsAtOpen.has(tag) && "rise-in")}
                  >
                    <button
                      type="button"
                      aria-pressed={held.has(tag)}
                      className="flex min-h-7 flex-1 cursor-pointer items-center gap-1.5 truncate rounded-sm px-1.5 text-left transition-colors duration-fast ease-out hover:bg-secondary aria-pressed:text-foreground"
                      onClick={() => apply(tag)}
                    >
                      <span
                        aria-hidden="true"
                        className={cn(
                          "w-3 text-primary opacity-0 transition-opacity duration-fast ease-out",
                          held.has(tag) && "opacity-100",
                        )}
                      >
                        ✓
                      </span>
                      {tag}
                    </button>
                    <button
                      type="button"
                      aria-label={`Remove ${tag} from search`}
                      title="Remove tag from search"
                      className="inline-grid size-6 cursor-pointer place-items-center rounded-full text-muted-foreground transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground"
                      onClick={() => onRemove(tag)}
                    >
                      {CLOSE_MARK}
                    </button>
                  </span>
                ))}
                {canCreate ? (
                  <button
                    key="create"
                    type="button"
                    className={cn(
                      "min-h-7 cursor-pointer rounded-sm px-1.5 text-left font-semibold text-primary transition-colors duration-fast ease-out hover:bg-secondary",
                      "rise-in",
                    )}
                    onClick={() => apply(typed)}
                  >
                    Create “{typed}”
                  </button>
                ) : null}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
