import type { ToastMessage } from "@/components/shared"
import { yourScore, type QueuedScore } from "@/lib/searches/feedback"
import type { ResultRow } from "@/lib/searches/ranking"
import { isPinned, PIN_TAG } from "@/lib/searches/tags"
import type { Candidate, FeedbackRecord, Ratings } from "@/types/searches"

import { ScoreDialog } from "../dialogs/ScoreDialog"
import type { Feedback } from "../hooks/useFeedback"
import type { SearchTags } from "../hooks/useSearchTags"
import { TagEditor } from "../toolbar/TagEditor"
import { PinButton } from "./PinButton"

/** What every row's controls share for one run; RunView memoizes it so rows re-render only
 *  when it changes (a tag edit, a queued score), not on the page's other renders. */
export interface RowContext {
  runId: string
  rubric: Ratings["rubric"]
  tags: SearchTags
  queued: ReadonlyMap<string, QueuedScore>
  submit: Feedback["submit"]
  onSaved: (record: FeedbackRecord) => void
  onToast: (toast: ToastMessage) => void
}

interface RowActionsProps {
  result: ResultRow
  candidate: Candidate
  context: RowContext
}

const NO_TAGS: readonly string[] = []

// A person's controls at the row's end: tags, pin, score. Only a run's candidates carry them:
// the server tags and scores those alone.
export function RowActions({ result, candidate, context }: RowActionsProps) {
  const { tags } = context
  const id = candidate.person_id
  const applied = tags.tagged.assignments[id] ?? NO_TAGS
  const score = yourScore(result, context.queued)
  return (
    <>
      <TagEditor
        personName={candidate.name}
        tags={tags.tagged.tags}
        applied={applied}
        disabled={tags.loading}
        onToggle={(tag) => tags.toggle(id, tag)}
        onRemove={tags.remove}
      />
      <PinButton
        name={candidate.name}
        pinned={isPinned(tags.tagged, id)}
        disabled={tags.loading}
        onToggle={() => tags.toggle(id, PIN_TAG)}
      />
      <ScoreDialog
        runId={context.runId}
        candidate={candidate}
        rubric={context.rubric}
        score={score?.score ?? null}
        note={score?.note ?? ""}
        onSaved={context.onSaved}
        submit={context.submit}
        onToast={context.onToast}
      />
    </>
  )
}
