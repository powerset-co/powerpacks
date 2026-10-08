import { useEffect, useRef, useState, type KeyboardEvent } from "react"

import { Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"

import { SendIcon, StopIcon } from "./icons"

const MAX_HEIGHT_PX = 220

interface ComposerProps {
  running: boolean
  canReset: boolean
  onSend: (text: string) => void
  onStop: () => void
  onReset: () => void
}

/** The message box: Enter sends, Shift+Enter breaks the line; Stop interrupts a running turn. */
export function Composer({ running, canReset, onSend, onStop, onReset }: ComposerProps) {
  const [text, setText] = useState("")
  const box = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const element = box.current
    if (!element) return
    element.style.height = "auto"
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT_PX)}px`
  }, [text])

  useEffect(() => box.current?.focus(), [])

  const submit = () => {
    const message = text.trim()
    if (!message || running) return
    onSend(message)
    setText("")
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="border-t border-line bg-[color-mix(in_srgb,var(--background)_92%,transparent)] px-5 pb-4 pt-3 backdrop-blur-[10px]">
      <form
        className="mx-auto flex max-w-[760px] flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <div className="flex items-end gap-2 rounded-[var(--radius-l)] border border-line-strong bg-card py-2 pl-3.5 pr-2 shadow-[var(--shadow-1)] transition-colors duration-fast ease-out focus-within:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]">
          <label htmlFor="agent-message" className="sr-only">
            Message
          </label>
          <textarea
            id="agent-message"
            ref={box}
            rows={1}
            value={text}
            placeholder="Ask Powerpacks to search, import or build context…"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            className="max-h-[220px] min-h-[28px] flex-1 resize-none border-0 bg-transparent py-1 text-[13.5px] leading-[1.5] text-foreground outline-none placeholder:text-faint focus-visible:outline-none"
          />
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
              disabled={!text.trim()}
              aria-label="Send"
            >
              <SendIcon />
            </Button>
          )}
        </div>
        <div className="flex min-h-6 items-center justify-between text-[11px] text-faint">
          <span className="flex items-center gap-1.5">
            <Kbd>Enter</Kbd> send <Kbd>Shift</Kbd>
            <Kbd>Enter</Kbd> new line
          </span>
          {canReset && (
            <Button type="button" variant="ghost" size="sm" onClick={onReset} disabled={running}>
              New conversation
            </Button>
          )}
        </div>
      </form>
    </div>
  )
}
