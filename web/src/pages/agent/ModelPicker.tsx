import { useQuery } from "@tanstack/react-query"
import { Check, ChevronRight } from "lucide-react"
import { useState, type ReactNode } from "react"

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { frontier } from "@/lib/agent/models"
import { setEffort, setModel, useAgent } from "@/lib/agent/store"
import { fetchModels, type CodexModel } from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"

const MODELS_KEY = ["codex-models"] as const
const ROW =
  "flex w-full cursor-pointer items-center gap-3 rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:bg-secondary data-[open=true]:bg-secondary"

type Flyout = "effort" | "more" | null

function ModelRow({ model, picked, onPick }: { model: CodexModel; picked: boolean; onPick: () => void }) {
  return (
    <li>
      <button type="button" className={ROW} title={model.description} onClick={onPick}>
        <span className="flex-1 truncate">{model.name}</span>
        {picked ? <Check className="size-4 text-primary" aria-hidden /> : null}
      </button>
    </li>
  )
}

/** A row that flies a panel out beside it on hover, as the Claude app's Effort row does. The
 *  panel is part of the row's own box, so the pointer can cross from the row to the panel
 *  without it closing; the menu sits at the bottom of the chat, so the panel grows upward. */
function FlyoutRow({
  open,
  onOpen,
  label,
  children,
  panel,
}: {
  open: boolean
  onOpen: (open: boolean) => void
  label: string
  children?: ReactNode
  panel: ReactNode
}) {
  return (
    <div className="relative" onMouseEnter={() => onOpen(true)} onMouseLeave={() => onOpen(false)}>
      <button
        type="button"
        className={ROW}
        data-open={open}
        aria-expanded={open}
        onClick={() => onOpen(true)}
      >
        <span className="flex-1">{label}</span>
        {children}
        <ChevronRight className="size-4 text-faint" aria-hidden />
      </button>
      {open ? (
        <div role="menu" aria-label={label} className="absolute bottom-[-6px] left-full z-10 w-[264px] pl-2">
          <div className="pop-in rounded-md border bg-popover p-1.5 text-popover-foreground shadow-md">
            {panel}
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** One pill, "GPT-6.1-Sol  Low": the model Codex answers with and how hard it thinks. It opens
 *  upward with the newest generations of the app-server's model list (model/list), a check on
 *  the pick, an Effort row that flies out the picked model's efforts (its default marked
 *  Recommended) and a More models row that flies out the older generations. */
export function ModelPicker() {
  const agent = useAgent()
  const [open, setOpen] = useState(false)
  const [flyout, setFlyout] = useState<Flyout>(null)
  const models = useQuery({ queryKey: MODELS_KEY, queryFn: fetchModels, staleTime: Infinity })
  const list = models.data ?? []
  // The list could not be read: say why in the pill's place, so the cause is on screen.
  if (models.error)
    return (
      <span className="truncate text-[11px] text-bad" title={errorText(models.error)}>
        Models: {errorText(models.error)}
      </span>
    )
  const picked =
    list.find((model) => model.id === agent.model) ?? list.find((model) => model.isDefault) ?? list[0]
  if (!picked) return null
  const effort = picked.efforts.some((option) => option.id === agent.effort)
    ? agent.effort
    : picked.defaultEffort
  const front = frontier(list)
  const shown = front.includes(picked) ? front : [...front, picked]
  const more = list.filter((model) => !shown.includes(model))
  const show = (next: boolean) => {
    setOpen(next)
    if (!next) setFlyout(null)
  }
  const pick = (model: CodexModel) => {
    setModel(model.id)
    show(false)
  }
  const flyoutOpen = (which: Flyout) => (next: boolean) =>
    setFlyout(next ? which : flyout === which ? null : flyout)
  return (
    <Popover open={open} onOpenChange={show}>
      <PopoverTrigger
        aria-label="Model and effort"
        title="The model Codex answers with, and how hard it thinks"
        className="inline-flex h-[26px] cursor-pointer items-center gap-1.5 rounded-full border border-line bg-transparent px-2.5 text-[11px] text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground"
      >
        <span className="truncate">{picked.name}</span>
        {effort ? <span className="capitalize text-faint">{effort}</span> : null}
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        onOpenAutoFocus={(event) => event.preventDefault()}
        className="w-56 p-1.5"
      >
        <ul className="m-0 flex list-none flex-col p-0">
          {shown.map((model) => (
            <ModelRow
              key={model.id}
              model={model}
              picked={model.id === picked.id}
              onPick={() => pick(model)}
            />
          ))}
        </ul>
        {picked.efforts.length > 0 ? (
          <>
            <div className="my-1.5 border-t border-line" />
            <FlyoutRow
              open={flyout === "effort"}
              onOpen={flyoutOpen("effort")}
              label="Effort"
              panel={
                <>
                  <p className="m-0 px-2.5 pb-2 pt-1 text-xs text-muted-foreground">
                    Higher effort means more thorough answers, but takes longer and uses your limits faster.
                  </p>
                  <ul className="m-0 flex list-none flex-col p-0">
                    {picked.efforts.map((option) => (
                      <li key={option.id}>
                        <button
                          type="button"
                          className={ROW}
                          title={option.description}
                          onClick={() => {
                            setEffort(option.id)
                            show(false)
                          }}
                        >
                          <span className="capitalize">{option.id}</span>
                          {option.id === picked.defaultEffort ? (
                            <span className="rounded-full border border-line px-1.5 text-[10px] text-muted-foreground">
                              Recommended
                            </span>
                          ) : null}
                          <span className="flex-1" />
                          {option.id === effort ? (
                            <Check className="size-4 text-primary" aria-hidden />
                          ) : null}
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              }
            >
              <span className="capitalize text-muted-foreground">{effort}</span>
            </FlyoutRow>
          </>
        ) : null}
        {more.length > 0 ? (
          <>
            <div className="my-1.5 border-t border-line" />
            <FlyoutRow
              open={flyout === "more"}
              onOpen={flyoutOpen("more")}
              label="More models"
              panel={
                <ul className="m-0 flex list-none flex-col p-0">
                  {more.map((model) => (
                    <ModelRow
                      key={model.id}
                      model={model}
                      picked={model.id === picked.id}
                      onPick={() => pick(model)}
                    />
                  ))}
                </ul>
              }
            />
          </>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}
