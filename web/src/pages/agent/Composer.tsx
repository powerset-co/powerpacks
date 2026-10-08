import { useEffect, useRef, useState, type KeyboardEvent } from "react"

import { Button } from "@/components/ui/button"

import { SendIcon, StopIcon } from "./icons"

const MAX_HEIGHT_PX = 240

interface ComposerProps {
  running: boolean
  onSend: (text: string) => void
  onStop: () => void
}

/** The message box: Enter sends, Shift+Enter breaks the line; Stop interrupts a running turn. */
export function Composer({ running, onSend, onStop }: ComposerProps) {
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
    <form
      className="mx-auto w-full max-w-[760px] px-5 pb-5 max-[680px]:px-3 max-[680px]:pb-3"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
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
          placeholder="Ask about anyone in your network…"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          className="max-h-[240px] min-h-[52px] resize-none border-0 bg-transparent px-4 pb-1 pt-3.5 text-[14px] leading-[1.5] text-foreground outline-none placeholder:text-faint focus-visible:outline-none"
        />
        <div className="flex items-center justify-between gap-3 px-3 pb-3">
          <span className="pl-1 text-[11px] text-faint">Searches with Powerpacks on this Mac</span>
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
      </div>
    </form>
  )
}
