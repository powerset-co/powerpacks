import { useEffect, useRef } from "react"

import { Select } from "@/components/shared"
import { plural } from "@/lib/copy"
import { LOGBOOK } from "@/lib/people/copy"
import { monthsByYear, type ReaderMonth } from "@/lib/people/logbook"

interface TimelineProps {
  months: readonly ReaderMonth[]
  // The month of the first message in view.
  active: string
  onJump: (month: ReaderMonth) => void
  onOldest: () => void
  onNewest: () => void
}

/**
 * The conversation's months, grouped by year, each as long a bar as it has messages; picking one
 * jumps to its first day. Narrow screens get the same months as one select. The month in view
 * stays marked, and scrolled into the list's view, as the reader scrolls.
 */
export function Timeline({ months, active, onJump, onOldest, onNewest }: TimelineProps) {
  const list = useRef<HTMLDivElement>(null)
  const busiest = Math.max(...months.map((month) => month.messages))
  const years = monthsByYear(months)

  useEffect(() => {
    const box = list.current
    const current = box?.querySelector<HTMLElement>("[aria-current='true']")
    if (!box || !current) return
    const top = current.offsetTop
    if (top < box.scrollTop || top + current.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = top - box.clientHeight / 2
    }
  }, [active])

  return (
    <nav className="logbook-timeline" aria-label="Timeline">
      <button type="button" className="logbook-edge" onClick={onOldest}>
        {LOGBOOK.oldest}
      </button>
      <Select
        className="logbook-jump"
        aria-label={LOGBOOK.jump}
        value={active}
        onChange={(key) => {
          const month = months.find((row) => row.key === key)
          if (month) onJump(month)
        }}
        options={years.map(([year, rows]) => ({
          label: year || "Undated",
          options: rows.map((month) => ({ value: month.key, label: `${month.label} ${month.year}`.trim() })),
        }))}
      />
      <div className="logbook-years" ref={list}>
        {years.map(([year, rows]) => (
          <section key={year} className="logbook-year">
            {year ? <h3>{year}</h3> : null}
            <ul>
              {rows.map((month) => (
                <li key={month.key}>
                  <button
                    type="button"
                    className="logbook-month"
                    aria-current={month.key === active || undefined}
                    aria-label={`${month.label} ${month.year}, ${plural(month.messages, "message")}`.trim()}
                    style={{ "--share": month.messages / busiest }}
                    onClick={() => onJump(month)}
                  >
                    <span>{month.label}</span>
                    <span className="logbook-month-count">{month.messages.toLocaleString()}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <button type="button" className="logbook-edge" onClick={onNewest}>
        {LOGBOOK.newest}
      </button>
    </nav>
  )
}
