import { ChevronLeft, ChevronRight, Info } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

import { mountStory, STEPS } from "./story"
import "./story.css"

// The stage's width: as wide as the window and its height allow. Everything in the dialog shares it.
const STAGE_WIDTH = "min(1000px, calc(100vw - 80px), calc((100dvh - 380px) * 1000 / 560))"

/** How Deep Context works: what stays on the Mac, what goes out and what comes back, playing step
 *  after step until Continue closes it. Every step's words share one slot, so nothing jumps. */
export function HowItWorks({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // The dialog adds its content a render after it opens, so the story starts when the stage does.
  const [stage, setStage] = useState<HTMLDivElement | null>(null)
  const story = useRef<ReturnType<typeof mountStory> | null>(null)
  const [step, setStep] = useState(0)
  useEffect(() => {
    if (!stage) return
    const mounted = mountStory(stage, setStep)
    story.current = mounted
    mounted.show(0)
    return () => {
      mounted.destroy()
      story.current = null
    }
  }, [stage])
  const go = (to: number) => story.current?.show(to)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="gap-5 p-6"
        style={{ width: `calc(${STAGE_WIDTH} + 48px)` }}
        // Nothing is focused on open, so no ring shows until someone tabs.
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="text-[17px]">How Deep Context works</DialogTitle>
          <DialogDescription className="text-[13px]">
            What stays on your Mac, what is sent out, and what comes back.
          </DialogDescription>
        </DialogHeader>
        <div
          ref={setStage}
          className="dck w-full shrink-0"
          role="img"
          aria-label="Animation: how Deep Context reads, sends and saves your data"
        />
        <div className="grid">
          {STEPS.map((item, at) => (
            <div
              key={item.title}
              aria-hidden={at !== step}
              className={cn(
                "col-start-1 row-start-1 flex flex-col gap-1 transition-[opacity,visibility] duration-300",
                at === step ? "visible opacity-100" : "invisible opacity-0",
              )}
            >
              <h3 className="m-0 text-base font-semibold">{item.title}</h3>
              <p className="m-0 max-w-[62ch] text-[13px] text-muted-foreground">{item.text}</p>
              {item.foot ? (
                <p className="m-0 mt-auto flex max-w-[75ch] items-start gap-2 pt-3 text-xs text-faint">
                  <Info className="mt-0.5 size-3.5 shrink-0" />
                  {item.foot}
                </p>
              ) : null}
            </div>
          ))}
        </div>
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              shape="pill"
              aria-label="Previous step"
              onClick={() => go(step - 1)}
            >
              <ChevronLeft />
            </Button>
            <div className="flex gap-1.5">
              {STEPS.map((item, at) => (
                <button
                  key={item.title}
                  type="button"
                  aria-label={`Step ${at + 1}: ${item.title}`}
                  aria-current={at === step ? "step" : undefined}
                  onClick={() => go(at)}
                  className={cn(
                    "h-1 w-5 cursor-pointer rounded-full border-0 p-0 transition-colors duration-200 max-[600px]:w-3",
                    at === step ? "bg-foreground" : "bg-line-strong hover:bg-muted-foreground",
                  )}
                />
              ))}
            </div>
            <Button
              variant="ghost"
              size="icon"
              shape="pill"
              aria-label="Next step"
              onClick={() => go(step + 1)}
            >
              <ChevronRight />
            </Button>
          </div>
          <Button variant="primary" size="lg" onClick={() => onOpenChange(false)}>
            Continue
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
