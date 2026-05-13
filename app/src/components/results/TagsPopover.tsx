/**
 * Popover for assigning tags to a person.
 *
 * - Type + Enter to create a new tag (or click "Create …" row).
 * - Click an existing tag to toggle its application on this person.
 * - Click the small X next to a tag to remove it from the conversation
 *   entirely (drops it from every person who had it).
 */

import { useEffect, useRef, useState, KeyboardEvent } from "react";
import { Check, X } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface TagsPopoverProps {
  /** All tags available in this conversation (canonical, ordered). */
  tags: string[];
  /** Tags currently applied to the person we're editing. */
  appliedTags: string[];
  /** Toggle a tag on the person (creates the tag if it doesn't exist). */
  onToggle: (tag: string) => void;
  /** Remove a tag from the entire conversation. */
  onRemoveTag: (tag: string) => void;
  /** Trigger element (click-to-open). */
  children: React.ReactNode;
  align?: "start" | "center" | "end";
  side?: "top" | "right" | "bottom" | "left";
}

export function TagsPopover({
  tags,
  appliedTags,
  onToggle,
  onRemoveTag,
  children,
  align = "end",
  side = "bottom",
}: TagsPopoverProps) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      // Defer focus until the popover content is mounted.
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
    setInput("");
  }, [open]);

  const trimmed = input.trim();
  const lower = trimmed.toLowerCase();
  const filtered = trimmed
    ? tags.filter((t) => t.toLowerCase().includes(lower))
    : tags;
  const exactMatch = trimmed && tags.some((t) => t.toLowerCase() === lower);
  const canCreate = trimmed.length > 0 && !exactMatch;
  const appliedSet = new Set(appliedTags.map((t) => t.toLowerCase()));

  const handleEnter = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (canCreate) {
      onToggle(trimmed);
      setInput("");
    } else if (filtered.length === 1) {
      onToggle(filtered[0]);
      setInput("");
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        asChild
        onClick={(e) => {
          // Don't trigger row click handlers / select-all etc.
          e.stopPropagation();
        }}
      >
        {children}
      </PopoverTrigger>
      <PopoverContent
        className="w-64 p-2"
        align={align}
        side={side}
        onClick={(e) => e.stopPropagation()}
        // Prevent the parent table row's keyboard handlers from firing.
        onKeyDown={(e) => e.stopPropagation()}
      >
        <Input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleEnter}
          placeholder="Add tag..."
          className="h-8 text-xs"
        />
        <div className="mt-2 max-h-56 overflow-y-auto">
          {filtered.length === 0 && !canCreate && (
            <p className="text-xs text-muted-foreground py-2 px-1">
              {tags.length === 0
                ? "No tags yet — type to create one."
                : "No matching tags"}
            </p>
          )}
          {filtered.map((tag) => {
            const applied = appliedSet.has(tag.toLowerCase());
            return (
              <div
                key={tag}
                className={cn(
                  "group flex items-center justify-between gap-2 rounded px-2 py-1 cursor-pointer text-sm",
                  applied ? "bg-primary/10 hover:bg-primary/15" : "hover:bg-muted"
                )}
                onClick={() => onToggle(tag)}
              >
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  <Check
                    className={cn(
                      "h-3.5 w-3.5 shrink-0",
                      applied ? "text-primary" : "text-transparent"
                    )}
                  />
                  <span className="truncate">{tag}</span>
                </div>
                <button
                  className="text-muted-foreground/40 hover:text-destructive shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRemoveTag(tag);
                  }}
                  title="Remove tag from conversation"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            );
          })}
          {canCreate && (
            <div
              className={cn(
                "flex items-center gap-2 rounded px-2 py-1 cursor-pointer hover:bg-muted text-sm",
                filtered.length > 0 && "border-t mt-1 pt-2"
              )}
              onClick={() => {
                onToggle(trimmed);
                setInput("");
              }}
            >
              <span className="text-xs text-muted-foreground">Create</span>
              <span className="font-medium truncate">"{trimmed}"</span>
            </div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
