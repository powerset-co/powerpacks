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
