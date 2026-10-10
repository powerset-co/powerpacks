import { useState } from "react"
import { Link } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { readStored, writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"

import { AsciiForm } from "./AsciiForm"
import { CODEX_FORMS, FORMS, KNOT, OPUS_FORMS } from "./forms"

export function VariationsPage() {
  const [selected, select] = useState(
    () =>
      readStored("local", "home.form", (name) => FORMS.find((form) => form.name === name) ?? null) ?? KNOT,
  )
  const [paused, pause] = useState(false)
  const [speed, setSpeed] = useState(1)
  return (
    <main className="min-h-dvh bg-background p-5 text-foreground sm:p-8">
      <header className="mx-auto mb-6 flex max-w-[1440px] flex-wrap items-center justify-between gap-4">
        <div>
          <p className="section-title">powerset % $ / motion studies</p>
          <h1 className="mb-0 mt-2 text-2xl font-semibold">Pick your strange little universe.</h1>
        </div>
      </header>
      <div className="mx-auto grid max-w-[1440px] gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="self-start overflow-hidden rounded-xl border border-line bg-card lg:sticky lg:top-6">
          <div className="flex items-center justify-between border-b border-line px-5 py-3">
            <span className="font-mono text-xs text-muted-foreground">powerset%$ · {selected.author}</span>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={() => pause(!paused)}>
                {paused ? "Play" : "Pause"}
              </Button>
              <Button asChild variant="primary">
                <Link to="/home" onClick={() => writeStored("local", "home.form", selected.name)}>
                  Use {selected.name}
                </Link>
              </Button>
            </div>
          </div>
          <AsciiForm form={selected} paused={paused} speed={speed} />
          <div className="border-t border-line p-5">
            <h2 className="m-0 text-xl font-semibold">{selected.name}</h2>
            <p className="mb-5 mt-2 text-sm text-muted-foreground">{selected.description}</p>
            <label className="flex items-center gap-3 text-xs text-muted-foreground">
              Speed{" "}
              <input
                aria-label="Animation speed"
                className="accent-[var(--primary)]"
                type="range"
                min=".25"
                max="2"
                step=".25"
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
              />
              <span className="font-mono">{speed}×</span>
            </label>
            <p className="mb-0 mt-4 text-xs text-faint">
              Move your cursor over the form to tilt it. Reduced-motion settings are respected.
            </p>
          </div>
        </section>
        <aside aria-label="Animation variations" className="space-y-6">
          {[CODEX_FORMS, OPUS_FORMS].map((forms, group) => (
            <section key={group}>
              <h2 className="section-title mb-3">{group === 0 ? "Codex" : "Opus 5.5"} / seven ideas</h2>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-1">
                {forms.map((form, index) => (
                  <button
                    key={form.name}
                    aria-pressed={selected === form}
                    onClick={() => select(form)}
                    className={cn(
                      "flex cursor-pointer gap-3 rounded-lg border bg-card p-3 text-left transition-colors hover:bg-secondary",
                      selected === form ? "border-primary" : "border-line",
                    )}
                  >
                    <span className="pt-0.5 font-mono text-xs text-primary">
                      {String(group * 7 + index + 1).padStart(2, "0")}
                    </span>
                    <span>
                      <span className="block text-sm font-semibold">{form.name}</span>
                      <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
                        {form.description}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </aside>
      </div>
    </main>
  )
}
