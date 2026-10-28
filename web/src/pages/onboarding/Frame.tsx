import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import type { ReactNode } from "react"
import { useNavigate } from "react-router-dom"

import { DEBUG_KEY, fetchDebugState } from "@/lib/api/debug"
import { isDesktop } from "@/lib/desktop"
import { cn } from "@/lib/utils"

import { STEPS, type StepKey } from "./steps"

/** The onboarding window: the steps across the top with a line under the ones reached, then two
 *  columns: what to do on the left, what it is about on the right. No Back and no Skip: each
 *  screen moves on by its own action. The steps are plain labels in a release build; a test build
 *  (the debug menu on) and the browser view make them links, a way around while working on them. */
export function Frame({ step, left, right }: { step: StepKey; left: ReactNode; right: ReactNode }) {
  const navigate = useNavigate()
  const index = STEPS.findIndex((item) => item.key === step)
  const debug = useQuery({ queryKey: DEBUG_KEY, queryFn: fetchDebugState, enabled: isDesktop() })
  const linked = !isDesktop() || debug.data?.enabled === true
  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground" data-onboarding={step}>
      <header className="relative flex h-[52px] shrink-0 items-center justify-center border-b border-line">
        <ol
          aria-label="Onboarding steps"
          className="m-0 flex list-none items-center gap-3 p-0 text-[11px] font-semibold uppercase tracking-[.1em]"
        >
          {STEPS.map((item, at) => (
            <li key={item.key} className="flex items-center gap-3">
              {linked ? (
                <button
                  type="button"
                  aria-current={at === index ? "step" : undefined}
                  className={cn(
                    "cursor-pointer border-0 bg-transparent p-0 font-semibold uppercase tracking-[.1em] hover:text-foreground",
                    at === index ? "text-foreground" : "text-faint",
                  )}
                  onClick={() => void navigate(item.href)}
                >
                  {item.label}
                </button>
              ) : (
                <span
                  aria-current={at === index ? "step" : undefined}
                  className={at === index ? "text-foreground" : "text-faint"}
                >
                  {item.label}
                </span>
              )}
              {at < STEPS.length - 1 ? <ChevronRight className="size-3.5 text-faint" aria-hidden /> : null}
            </li>
          ))}
        </ol>
        <div
          aria-hidden
          className="absolute bottom-[-1px] left-0 h-[2px] bg-primary transition-[width] duration-slow ease-out"
          style={{ width: `${((index + 1) / STEPS.length) * 100}%` }}
        />
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-2 max-[720px]:grid-cols-1">
        <section className="relative flex min-h-0 flex-col overflow-y-auto px-10 py-8">
          <div className="mx-auto mt-[12vh] flex w-full max-w-[380px] flex-col gap-5 pb-12">{left}</div>
        </section>
        <aside className="relative flex min-h-0 flex-col overflow-y-auto border-l border-line bg-card px-10 py-8">
          <div className="mx-auto mt-[12vh] flex min-h-0 w-full max-w-[380px] flex-1 flex-col gap-5 pb-12">
            {right}
          </div>
        </aside>
      </div>
    </div>
  )
}

/** The left column's heading: the title, then one line under it. */
export function Heading({ title, line }: { title: ReactNode; line?: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <h1 className="m-0 text-[22px] font-bold leading-tight">{title}</h1>
      {line ? <p className="m-0 text-[13px] text-muted-foreground">{line}</p> : null}
    </div>
  )
}

/** The right column's information: a title and a few lines. */
export function Info({
  title,
  lines,
  note,
}: {
  title: string
  lines: readonly ReactNode[]
  note?: ReactNode
}) {
  return (
    <div className="flex flex-col gap-4">
      <h2 className="m-0 text-[15px] font-semibold">{title}</h2>
      <ul className="m-0 flex list-none flex-col gap-2.5 p-0 text-[13px]">
        {lines.map((line, at) => (
          <li key={at} className="flex gap-3">
            <span className="mt-[7px] size-1.5 shrink-0 rounded-full bg-primary" aria-hidden />
            <span>{line}</span>
          </li>
        ))}
      </ul>
      {note ? <p className="m-0 text-xs text-muted-foreground">{note}</p> : null}
    </div>
  )
}

/** A quiet line of the column's own, under its action. */
export function Note({ children, bad = false }: { children: ReactNode; bad?: boolean }) {
  return (
    <p
      className={cn("m-0 text-xs", bad ? "text-bad" : "text-muted-foreground")}
      role={bad ? "alert" : undefined}
    >
      {children}
    </p>
  )
}
