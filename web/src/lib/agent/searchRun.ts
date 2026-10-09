// The search a chat is about: its run folder appears in the commands Codex ran
// (`--output-dir .powerpacks/search/<slug>` for a quick search, `.powerpacks/deep-search/<run>`
// for a deep one) or in the first message of a chat opened from a saved search. Quick
// searches are saved under the same name as one-pond runs.

import type { Entry } from "@/types/agent"

const RUN_DIR = /\.powerpacks\/(?:deep-)?search\/([A-Za-z0-9._-]+)/g

/** The chat's newest run that the catalog lists, or null. */
export function chatRun(entries: readonly Entry[], listed: ReadonlySet<string>): string | null {
  const named = entries.flatMap((entry) =>
    entry.kind === "command" || entry.kind === "user"
      ? [...(entry.kind === "command" ? entry.command : entry.text).matchAll(RUN_DIR)].map(
          (match) => match[1] ?? "",
        )
      : [],
  )
  return named.filter((runId) => listed.has(runId)).at(-1) ?? null
}
