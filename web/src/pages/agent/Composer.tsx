import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react"

import { isDesktop } from "@/lib/desktop"
import { AutoReply } from "./AutoReply"
import { ComposerToggle } from "./ComposerToggle"

import { Button } from "@/components/ui/button"

import { SendIcon, StopIcon } from "./icons"

const MAX_HEIGHT_PX = 240

export interface ComposerProps {
  running: boolean
  fullAccess?: boolean
  onSend: ((text: string) => void) | ((text: string) => Promise<boolean>)
  onStop?: () => void
  onFullAccess?: (on: boolean) => void
  footer?: ReactNode
  placeholder?: string
  draft?: string
  initialText?: string
  onDraftChange?: (text: string) => void
  disabled?: boolean
}

/** The message box: Enter sends, Shift+Enter breaks the line; Stop interrupts a running turn. */
export function Composer({
  running,
  fullAccess = false,
  onSend,
  onStop,
  onFullAccess,
  footer,
  placeholder,
  initialText = "",
  draft,
  onDraftChange,
  disabled = false,
}: ComposerProps) {
  const [localText, setText] = useState(initialText)
  const text = draft ?? localText
  const box = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const element = box.current
    if (!element) return
    element.style.height = "auto"
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT_PX)}px`
  }, [text])

  useEffect(() => box.current?.focus(), [])

  const submit = async () => {
    const message = text.trim()
    if (!message || running || disabled) return
    const sent = await onSend(message)
    if (sent === false) return
    setText("")
    onDraftChange?.("")
    box.current?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
  }

  return (
    <form
      className="mx-auto w-full max-w-[760px] px-5 pb-5 max-[680px]:px-3 max-[680px]:pb-3"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <div className="flex flex-col rounded-[20px] border border-line-strong bg-card shadow-[var(--shadow-2)] transition-colors duration-fast ease-out focus-within:border-[color-mix(in_srgb,var(--primary)_45%,var(--line-strong))]">
        <label htmlFor="agent-message" className="sr-only">
          Message
        </label>
        <textarea
          id="agent-message"
          ref={box}
          rows={1}
          value={text}
          placeholder={placeholder ?? "Ask about anyone in your network…"}
          onChange={(event) => {
            setText(event.target.value)
            onDraftChange?.(event.target.value)
          }}
          onKeyDown={onKeyDown}
          className="max-h-[240px] min-h-[52px] resize-none border-0 bg-transparent px-4 pb-1 pt-3.5 text-[14px] leading-[1.5] text-foreground outline-none placeholder:text-faint focus-visible:outline-none"
        />
        <div className="flex items-center justify-between gap-3 px-3 pb-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {footer}
            {onFullAccess && (
              <ComposerToggle
                label="Full Access"
                enabled={fullAccess}
                onChange={() => onFullAccess(!fullAccess)}
                description="Lets your Codex run commands without asking. Does not change permissions for shared replies."
              />
            )}
            {isDesktop() && <AutoReply />}
          </div>
          {running ? (
            <Button type="button" size="icon" shape="pill" onClick={onStop} aria-label="Stop">
              <StopIcon className="!size-3" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              shape="pill"
              variant="primary"
              disabled={disabled || !text.trim()}
              aria-label="Send"
            >
              <SendIcon />
            </Button>
          )}
        </div>
      </div>
    </form>
  )
}
