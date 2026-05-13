/**
 * FeedbackPopover — Inline data annotation widget
 *
 * Exports:
 * - FeedbackForm: standalone form (use inside dropdowns, panels, etc.)
 * - FeedbackPopover: wraps FeedbackForm in a Radix Popover
 * - FeedbackTriggerButton: reusable trigger icon
 *
 * Created: 2026-03-24
 * Changelog:
 * - 2026-03-25: Extracted FeedbackForm so it's reusable inside dropdowns
 */

import { useState, useRef, useEffect, useCallback } from "react";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import { Flag, Send, Check, MessageSquarePlus, CornerDownLeft } from "lucide-react";
import { cn } from "@/lib/utils";

/* ─── Types ─────────────────────────────────────────────────────── */

export interface FeedbackPayload {
  /** Auto-detected category — hidden from user */
  category: string;
  /** ID of the entity being annotated (person, company, etc.) */
  entityId: string;
  /** The current display value of the field */
  fieldValue: string;
  /** User's comment */
  comment: string;
}

export interface FeedbackFormProps {
  /** Auto-detected category (hidden from user) */
  category: string;
  /** Entity being annotated */
  entityId: string;
  /** Current value of the annotated field */
  fieldValue: string;
  /** Context shown at the top */
  contextLabel?: string;
  /** Fired when user submits feedback */
  onSubmit: (payload: FeedbackPayload) => void;
  /** Called after done animation completes (e.g. to close parent) */
  onDone?: () => void;
  /** Auto-focus the textarea on mount */
  autoFocus?: boolean;
  /** Additional className for the outer wrapper */
  className?: string;
}

export interface FeedbackPopoverProps extends Omit<FeedbackFormProps, "onDone" | "autoFocus" | "className"> {
  /** Custom trigger — defaults to a flag icon that fades in on group-hover */
  children?: React.ReactNode;
  /** Which side to align the popover */
  align?: "start" | "center" | "end";
}

/* ─── FeedbackForm (standalone, reusable) ───────────────────────── */

export function FeedbackForm({
  category,
  entityId,
  fieldValue,
  contextLabel,
  onSubmit,
  onDone,
  autoFocus = true,
  className,
}: FeedbackFormProps) {
  const [comment, setComment] = useState("");
  const [done, setDone] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (autoFocus && !done) {
      const timer = setTimeout(() => textareaRef.current?.focus(), 80);
      return () => clearTimeout(timer);
    }
  }, [autoFocus, done]);

  const submit = useCallback(() => {
    if (!comment.trim()) return;
    onSubmit({
      category,
      entityId,
      fieldValue,
      comment: comment.trim(),
    });
    setDone(true);
    if (onDone) setTimeout(onDone, 700);
  }, [category, entityId, fieldValue, comment, onSubmit, onDone]);

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setComment(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  };

  if (done) {
    return (
      <div className={cn("flex items-center gap-2 px-4 py-3 animate-in fade-in-0 zoom-in-95 duration-200", className)}>
        <div className="h-7 w-7 rounded-full flex items-center justify-center shrink-0 bg-green-100 dark:bg-green-900/30">
          <Check className="h-3.5 w-3.5 text-green-600 dark:text-green-400" />
        </div>
        <p className="text-sm font-medium">Got it, thanks! 🙏</p>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col p-3 gap-2", className)} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      {contextLabel && (
        <p className="text-[11px] font-medium truncate px-0.5 text-muted-foreground">
          {contextLabel}
        </p>
      )}

      <textarea
        ref={textareaRef}
        rows={2}
        placeholder='e.g. "Wrong person — this is actually Jane Smith"'
        value={comment}
        onChange={handleInput}
        className="w-full resize-none outline-none min-h-[48px] max-h-[140px] bg-transparent text-sm text-foreground placeholder:text-muted-foreground/40"
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
      />

      <div className="flex items-center justify-between text-muted-foreground/50">
        <div className="flex items-center gap-1 text-[10px]">
          <CornerDownLeft className="h-2.5 w-2.5" />
          <span>⌘+Enter</span>
        </div>
        <button
          onClick={submit}
          disabled={!comment.trim()}
          className="p-1.5 rounded-md bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground transition-colors disabled:opacity-30 disabled:pointer-events-none"
        >
          <Send className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

/* ─── FeedbackPopover (wraps FeedbackForm in a popover) ─────────── */

export function FeedbackPopover({
  category,
  entityId,
  fieldValue,
  contextLabel,
  onSubmit,
  children,
  align = "start",
}: FeedbackPopoverProps) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState(0);

  // Reset form when popover closes
  useEffect(() => {
    if (!open) {
      const timer = setTimeout(() => setKey((k) => k + 1), 250);
      return () => clearTimeout(timer);
    }
  }, [open]);

  return (
    <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
      <PopoverPrimitive.Trigger asChild>
        {children || (
          <button
            className="opacity-0 group-hover:opacity-100 transition-opacity duration-150 p-0.5 rounded hover:bg-muted"
            aria-label="Give feedback"
          >
            <Flag className="h-3 w-3 text-muted-foreground" />
          </button>
        )}
      </PopoverPrimitive.Trigger>

      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          sideOffset={8}
          className={cn(
            "z-50 w-72 overflow-hidden outline-none",
            "rounded-xl bg-popover text-popover-foreground",
            "shadow-[0_4px_24px_-4px_rgba(0,0,0,0.12)] dark:shadow-[0_4px_24px_-4px_rgba(0,0,0,0.5)]",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
            "data-[side=bottom]:slide-in-from-top-2 data-[side=top]:slide-in-from-bottom-2",
          )}
        >
          <FeedbackForm
            key={key}
            category={category}
            entityId={entityId}
            fieldValue={fieldValue}
            contextLabel={contextLabel}
            onSubmit={onSubmit}
            onDone={() => setOpen(false)}
          />
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

/* ─── Trigger Buttons ───────────────────────────────────────────── */

export function FeedbackTriggerButton({
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs",
        "text-muted-foreground hover:text-foreground",
        "hover:bg-muted/80 transition-colors duration-150",
        "opacity-0 group-hover:opacity-100",
        className,
      )}
      aria-label="Give feedback"
      {...props}
    >
      <MessageSquarePlus className="h-3 w-3" />
    </button>
  );
}
