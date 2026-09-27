import type { ToastMessage } from "@/components/shared"
import type { QueuedScore } from "@/lib/searches/feedback"
import { existingTag, PIN_TAG } from "@/lib/searches/tags"
import type { Candidate, FeedbackRecord, Ratings } from "@/types/searches"

import { ScoreDialog } from "../dialogs/ScoreDialog"
import type { Feedback } from "../hooks/useFeedback"
import type { SearchTags } from "../hooks/useSearchTags"
import { TagEditor } from "../toolbar/TagEditor"
import { PinButton } from "./PinButton"

interface RowActionsProps {
  runId: string
  candidate: Candidate
  rubric: Ratings["rubric"]
  tags: SearchTags
  score: QueuedScore | null
  submit: Feedback["submit"]
  onSaved: (record: FeedbackRecord) => void
  onToast: (toast: ToastMessage) => void
}

const NO_TAGS: readonly string[] = []

// A person's controls at the row's end: tags, pin, score. Only a run's candidates carry them:
// the server tags and scores those alone.
export function RowActions({
  runId,
  candidate,
  rubric,
  tags,
  score,
  submit,
  onSaved,
  onToast,
}: RowActionsProps) {
  const id = candidate.person_id
  const applied = tags.tagged.assignments[id] ?? NO_TAGS
  const pin = existingTag(tags.tagged.tags, PIN_TAG) ?? PIN_TAG
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
        pinned={applied.includes(pin)}
        disabled={tags.loading}
        onToggle={() => tags.toggle(id, pin)}
      />
      <ScoreDialog
        runId={runId}
        candidate={candidate}
        rubric={rubric}
        score={score?.score ?? null}
        note={score?.note ?? ""}
        onSaved={onSaved}
        submit={submit}
        onToast={onToast}
      />
    </>
  )
}
