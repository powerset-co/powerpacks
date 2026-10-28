import { useQuery } from "@tanstack/react-query"

import { Select, SelectContent, SelectItem, SelectTrigger } from "@/components/ui/select"
import { setEffort, setModel, useAgent } from "@/lib/agent/store"
import { fetchModels } from "@/lib/api/codex"

const MODELS_KEY = ["codex-models"] as const
const PILL =
  "h-[26px] w-auto gap-1.5 rounded-full border-line bg-transparent py-0 pl-2.5 pr-2 text-[11px] text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground focus:border-line-strong focus:ring-0 focus:ring-offset-0 [&>svg]:size-3 [&>svg]:shrink-0"

/** The model Codex answers with and how hard it thinks: the app-server's own list (model/list),
 *  Codex's default picked until the person picks another. The effort list is the picked model's. */
export function ModelPicker() {
  const agent = useAgent()
  const models = useQuery({ queryKey: MODELS_KEY, queryFn: fetchModels, staleTime: Infinity })
  const list = models.data ?? []
  if (list.length === 0) return null
  const picked =
    list.find((model) => model.id === agent.model) ?? list.find((model) => model.isDefault) ?? list[0]
  if (!picked) return null
  const effort = picked.efforts.some((option) => option.id === agent.effort)
    ? agent.effort
    : picked.defaultEffort
  return (
    <>
      <Select value={picked.id} onValueChange={(value) => setModel(value)}>
        <SelectTrigger aria-label="Model" title="The model Codex answers with" className={PILL}>
          <span className="truncate">{picked.name}</span>
        </SelectTrigger>
        <SelectContent align="start">
          {list.map((model) => (
            <SelectItem key={model.id} value={model.id} title={model.description}>
              {model.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {picked.efforts.length > 0 && effort ? (
        <Select value={effort} onValueChange={(value) => setEffort(value)}>
          <SelectTrigger aria-label="Effort" title="How hard the model thinks" className={PILL}>
            <span className="truncate capitalize">{effort}</span>
          </SelectTrigger>
          <SelectContent align="start">
            {picked.efforts.map((option) => (
              <SelectItem key={option.id} value={option.id} title={option.description} className="capitalize">
                {option.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : null}
    </>
  )
}
