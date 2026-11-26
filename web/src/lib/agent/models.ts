import type { CodexModel, TurnChoice } from "@/lib/api/codex"

// Which of the app-server's models the picker opens on: the newest two generations (gpt-6.1
// and gpt-6 today); More models shows the rest.

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

/** The model and effort a turn runs with, and the picker shows: the pick, else the user's own
 *  Codex default, else the app-server's default; each only when the signed-in account lists it,
 *  since Codex refuses a model outside the account's catalog. Null when nothing is listed. */
export function resolveChoice(
  list: CodexModel[],
  pick: TurnChoice,
  defaults: TurnChoice | undefined,
): { model: CodexModel; effort: string } | null {
  const model =
    list.find(({ id }) => id === pick.model) ??
    list.find(({ id }) => id === defaults?.model) ??
    list.find(({ isDefault }) => isDefault) ??
    list[0]
  if (!model) return null
  const offered = (id: string | null | undefined): id is string =>
    model.efforts.some((option) => option.id === id)
  const effort = offered(pick.effort)
    ? pick.effort
    : offered(defaults?.effort)
      ? defaults.effort
      : model.defaultEffort
  return { model, effort }
}
