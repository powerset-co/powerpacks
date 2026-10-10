import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"

import { useDismiss } from "@/hooks/useDismiss"
import { fetchDebugState, resetData, skipSetup } from "@/lib/api/debug"
import { errorText } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"

const DEBUG_KEY = ["desktop-debug"] as const

// Every page the app shows, including the two outside the top bar.
const PAGES = [
  { label: "Home", href: "/home" },
  { label: "Setup", href: "/install" },
  { label: "LinkedIn review", href: "/" },
  { label: "Chat", href: "/agent" },
  { label: "Searches", href: "/searches" },
  { label: "People", href: "/people" },
  { label: "Accounts", href: "/accounts" },
  { label: "Scheduled tasks", href: "/tasks" },
] as const

const ITEM =
  "flex w-full cursor-pointer items-center rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:bg-secondary"

/** Test builds of the desktop app: jump to any page, and skip setup to try the rest first. */
export function DebugMenu() {
  if (!isDesktop()) return null
  return <DesktopDebugMenu />
}

function DesktopDebugMenu() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const state = useQuery({ queryKey: DEBUG_KEY, queryFn: fetchDebugState })
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const panel = useRef<HTMLDivElement>(null)
  const anchor = useRef<HTMLButtonElement>(null)
  const close = useCallback(() => setOpen(false), [])
  useDismiss(open, panel, anchor, close)

  if (!state.data?.enabled) return null
  const skipped = state.data.setupSkipped

  const reset = () => {
    if (
      !window.confirm("Delete this app's data and start setup over? Your command-line install keeps its own.")
    )
      return
    close()
    setError(null)
    resetData()
      .then(() => {
        void client.invalidateQueries()
        void navigate("/install")
      })
      .catch((caught: unknown) => setError(errorText(caught)))
  }

  const toggleSkip = () => {
    setError(null)
    skipSetup(!skipped)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => void client.invalidateQueries({ queryKey: DEBUG_KEY }))
  }

  return (
    <div className="fixed bottom-4 right-4 z-[60] flex flex-col items-end gap-2">
      {open && (
        <div
          ref={panel}
          role="menu"
          aria-label="Debug"
          className="rise-in w-[220px] rounded-[var(--radius-m)] border border-line-strong bg-card p-1.5 shadow-[var(--shadow-2)]"
        >
          <p className="m-0 px-2.5 pb-1 pt-1.5 text-[10.5px] font-semibold uppercase tracking-[.08em] text-faint">
            Go to
          </p>
          {PAGES.map(({ label, href }) => (
            <button
              key={href}
              type="button"
              role="menuitem"
              className={ITEM}
              onClick={() => {
                close()
                void navigate(href)
              }}
            >
              {label}
            </button>
          ))}
          <div className="my-1.5 border-t border-line" />
          <label className="flex cursor-pointer items-center justify-between gap-3 rounded-[var(--radius-s)] px-2.5 py-1.5 text-[13px] hover:bg-secondary">
            Skip setup on launch
            <input
              type="checkbox"
              checked={skipped}
              onChange={toggleSkip}
              className="accent-[var(--primary)]"
            />
          </label>
          <button
            type="button"
            role="menuitem"
            className={ITEM.replace("text-foreground", "text-bad")}
            onClick={reset}
          >
            Delete app data…
          </button>
          {error && <p className="m-0 px-2.5 py-1 text-xs text-bad">{error}</p>}
        </div>
      )}
      <button
        ref={anchor}
        type="button"
        aria-label="Debug menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="grid size-8 cursor-pointer place-items-center rounded-full border border-line-strong bg-card font-mono text-[11px] font-bold text-muted-foreground shadow-[var(--shadow-2)] transition-colors duration-fast ease-out hover:text-foreground aria-expanded:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] aria-expanded:text-foreground"
      >
        {"{}"}
      </button>
    </div>
  )
}
