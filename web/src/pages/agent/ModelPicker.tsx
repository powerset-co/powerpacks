import { useQuery } from "@tanstack/react-query"
import { Check, ChevronLeft, ChevronRight } from "lucide-react"
import { useState } from "react"

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { setEffort, setModel, useAgent } from "@/lib/agent/store"
import { fetchModels } from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"
import { cn } from "@/lib/utils"

const MODELS_KEY = ["codex-models"] as const
// The list opens on the newest two generations (gpt-6.1 and gpt-6 today); More models shows the rest.
const GENERATION = /^gpt-(\d+(?:\.\d+)?)/
const SHOWN_GENERATIONS = 2

function generation(id: string): number {
  const match = GENERATION.exec(id)
  return match?.[1] ? Number(match[1]) : 0
}

/** The models of the newest generations; the whole list when it has nothing to hide. */
export function frontier<T extends { id: string }>(list: T[]): T[] {
  const newest = [...new Set(list.map((model) => generation(model.id)))]
    .sort((a, b) => b - a)
    .slice(0, SHOWN_GENERATIONS)
  return list.filter((model) => newest.includes(generation(model.id)))
}
const ROW =
  "flex w-full cursor-pointer items-center gap-3 rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors duration-fast ease-out hover:bg-secondary"

/** One pill, "GPT-6.1-Sol  Low": the model Codex answers with and how hard it thinks. It opens
 *  the app-server's own model list (model/list), with an Effort row that turns the panel into
 *  the picked model's efforts, its default marked Recommended. */
export function ModelPicker() {
  const agent = useAgent()
  const [open, setOpen] = useState(false)
  const [pane, setPane] = useState<"models" | "effort">("models")
  const [all, setAll] = useState(false)
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
  const show = (next: boolean) => {
    setOpen(next)
    if (!next) {
      setPane("models")
      setAll(false)
    }
  }
  const front = frontier(list)
  // The pick stays listed even when it is an older model.
  const shown = all ? list : front.includes(picked) ? front : [...front, picked]
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
      <PopoverContent align="start" side="top" className="w-64 p-1.5">
        {pane === "models" ? (
          <>
            <ul className="m-0 flex list-none flex-col p-0">
              {shown.map((model) => (
                <li key={model.id}>
                  <button
                    type="button"
                    className={ROW}
                    title={model.description}
                    onClick={() => {
                      setModel(model.id)
                      show(false)
                    }}
                  >
                    <span className="flex-1 truncate">{model.name}</span>
                    {model.id === picked.id ? <Check className="size-4 text-primary" aria-hidden /> : null}
                  </button>
                </li>
              ))}
            </ul>
            {picked.efforts.length > 0 ? (
              <>
                <div className="my-1.5 border-t border-line" />
                <button type="button" className={ROW} onClick={() => setPane("effort")}>
                  <span className="flex-1">Effort</span>
                  <span className="capitalize text-muted-foreground">{effort}</span>
                  <ChevronRight className="size-4 text-faint" aria-hidden />
                </button>
              </>
            ) : null}
            {!all && shown.length < list.length ? (
              <>
                <div className="my-1.5 border-t border-line" />
                <button type="button" className={ROW} onClick={() => setAll(true)}>
                  <span className="flex-1">More models</span>
                  <ChevronRight className="size-4 text-faint" aria-hidden />
                </button>
              </>
            ) : null}
          </>
        ) : (
          <>
            <button
              type="button"
              className={cn(ROW, "text-muted-foreground")}
              onClick={() => setPane("models")}
            >
              <ChevronLeft className="size-4" aria-hidden />
              <span className="flex-1">{picked.name}</span>
            </button>
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
                    {option.id === effort ? <Check className="size-4 text-primary" aria-hidden /> : null}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </PopoverContent>
    </Popover>
  )
}
