import { ActionBar, ActionBarRule, Kbd } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { RubricChoice } from "@/lib/searches/rubric"

const BAR_BUTTON = "min-h-[30px]"

interface ReviewBarProps {
  // The open candidate's name, or null: no bar.
  label: string | null
  choices: readonly RubricChoice[]
  // Their saved (or queued) score: its button is marked.
  score: number | null
  // Only a run's candidates take scores, tags and pins.
  scorable: boolean
  // While the saved tags load: a tag or pin then would overwrite them.
  tagsLoading: boolean
  pinned: boolean
  onScore: (score: number) => void
  onTag: () => void
  onPin: () => void
  onClose: () => void
}

// The open candidate's review, on the shared action bar: a rubric score (its digit), then the
// next candidate opens; Tag, Pin, Close.
export function ReviewBar(props: ReviewBarProps) {
  const { choices, score, scorable, tagsLoading } = props
  return (
    <ActionBar label={props.label} name="Review" className="review-bar">
      {choices.map((choice) => (
        <Button
          key={choice.score}
          variant={choice.score === score ? "primary" : "default"}
          shape="pill"
          className={BAR_BUTTON}
          aria-pressed={choice.score === score}
          aria-label={`${choice.score} ${choice.name}`}
          title={choice.meaning}
          disabled={!scorable}
          onClick={() => props.onScore(choice.score)}
        >
          <Kbd>{choice.score}</Kbd>
          <span className="review-choice-name">{choice.name}</span>
        </Button>
      ))}
      <ActionBarRule />
      <Button shape="pill" className={BAR_BUTTON} disabled={!scorable || tagsLoading} onClick={props.onTag}>
        Tag <Kbd className="ml-0.5">T</Kbd>
      </Button>
      <Button
        shape="pill"
        className={BAR_BUTTON}
        aria-pressed={props.pinned}
        disabled={!scorable || tagsLoading}
        onClick={props.onPin}
      >
        {props.pinned ? "Unpin" : "Pin"} <Kbd className="ml-0.5">P</Kbd>
      </Button>
      <ActionBarRule />
      <Button variant="ghost" shape="pill" className={BAR_BUTTON} onClick={props.onClose}>
        Close <Kbd className="ml-0.5">Esc</Kbd>
      </Button>
    </ActionBar>
  )
}
