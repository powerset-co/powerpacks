import { useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

import { flows, mountDemo, type GuideSource, type Person } from "./demos"
import "./guide.css"

const WIDTH = 640
const HEIGHT = 510

/** One source's guide in the right column: what the person is about to see, moving (the stage
 *  loops through its three steps), the steps under it, and the step under way's caption. A step
 *  is a click to its point in the loop. */
export function Guide({ source, person }: { source: GuideSource; person: Person }) {
  const flow = flows[source]
  const frame = useRef<HTMLDivElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const elapsed = useRef(0)
  const [scale, setScale] = useState(0.5)
  const [step, setStep] = useState(0)
  const [beat, setBeat] = useState(0)
  useEffect(() => {
    const element = frame.current
    if (!element) return
    const fit = () => setScale(element.clientWidth / WIDTH)
    fit()
    const watch = new ResizeObserver(fit)
    watch.observe(element)
    return () => watch.disconnect()
  }, [])
  useEffect(() => {
    const element = stage.current
    if (!element) return
    const demo = mountDemo(element, source, { person: { name: person.name, email: person.email } })
    // Beat 0 (the first sixth) is a still frame: start where the motion starts.
    elapsed.current = flow.duration / 6
    let last = performance.now()
    let handle = 0
    const tick = (now: number) => {
      elapsed.current = (elapsed.current + (now - last) / 1000) % flow.duration
      last = now
      const progress = elapsed.current / flow.duration
      demo.seek(progress)
      setStep(Math.min(2, Math.floor(progress * 3)))
      setBeat(Math.min(5, Math.floor(progress * 6)))
      handle = requestAnimationFrame(tick)
    }
    handle = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(handle)
  }, [source, person.name, person.email, flow.duration])
  const caption =
    source === "gmail" && beat === 4
      ? "Powerpacks ticks the two Gmail permissions and continues. If Google asks you instead, tick Select all, then Continue."
      : flow.captions[step]
  return (
    <div className="guide flex flex-col gap-4" data-guide={source}>
      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-semibold uppercase tracking-[.1em] text-faint">{flow.eyebrow}</span>
        <h2 className="m-0 text-[15px] font-semibold">{flow.title}</h2>
        <p className="m-0 text-[13px] text-muted-foreground">{flow.intro}</p>
      </div>
      <div ref={frame} className="w-full overflow-hidden rounded-[var(--radius-m)]" style={{ height: HEIGHT * scale }}>
        <div ref={stage} role="img" aria-label={`How ${flow.title}`} style={{ transform: `scale(${scale})` }} />
      </div>
      <p className="m-0 min-h-9 text-center text-xs text-muted-foreground" aria-live="off">
        {caption}
      </p>
      <ol className="m-0 flex list-none flex-col gap-1 p-0 text-[13px]">
        {flow.steps.map((line, at) => (
          <li key={line}>
            <button
              type="button"
              aria-current={at === step ? "step" : undefined}
              className={cn(
                "flex w-full cursor-pointer items-center gap-3 rounded-[var(--radius-s)] border-0 bg-transparent px-2 py-1.5 text-left",
                at === step ? "bg-surface-2 text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => {
                elapsed.current = (at * flow.duration) / 3 + 0.05
              }}
            >
              <span
                className={cn(
                  "grid size-6 shrink-0 place-items-center rounded-full border text-[11px]",
                  at === step ? "border-primary bg-primary text-white" : "border-line-strong",
                )}
              >
                {at + 1}
              </span>
              {line}
            </button>
          </li>
        ))}
      </ol>
      <p className="m-0 text-xs text-muted-foreground">{flow.note}</p>
    </div>
  )
}
