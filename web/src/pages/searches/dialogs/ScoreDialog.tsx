import {
  forwardRef,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type RefObject,
} from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { buildScoreFeedback, SCORE_SCALE } from "@/lib/searches/feedback"
import { rubricChoices, type RubricChoice } from "@/lib/searches/rubric"
import { cn } from "@/lib/utils"
import type { Candidate, FeedbackRecord, Ratings } from "@/types/searches"

import { FeedbackFooter } from "./FeedbackFooter"
import { NotesField } from "./NotesField"
import { send, type FeedbackSink } from "./send"

export interface ScoreDialogProps extends FeedbackSink {
  runId: string
  candidate: Pick<Candidate, "person_id" | "name" | "title" | "company">
  rubric: Ratings["rubric"]
  /** The score and note shown on the badge and preselected: saved, or still queued. */
  score: number | null
  note: string
  /** The row's badge takes the new score and note; called before the record is sent. */
  onSaved: (record: FeedbackRecord) => void
}

/** A candidate's score badge, which opens the five-point score dialog. */
export function ScoreDialog({
  runId,
  candidate,
  rubric,
  score,
  note,
  onSaved,
  submit,
  onToast,
}: ScoreDialogProps) {
  const [open, setOpen] = useState(false)
  const initial = useRef<HTMLInputElement>(null)
  const context = [candidate.title, candidate.company].filter(Boolean).join(" · ")

  function done(record: FeedbackRecord) {
    onSaved(record)
    setOpen(false)
    send({ submit, onToast }, record)
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <ScoreBadge score={score} name={candidate.name} />
      </DialogTrigger>
      <DialogContent
        className="w-[min(448px,calc(100%-32px))] gap-5 p-6"
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          initial.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">Score {candidate.name}</DialogTitle>
          <DialogDescription className="text-[13px]">{context}</DialogDescription>
        </DialogHeader>
        <ScoreForm
          choices={rubricChoices(rubric)}
          score={score}
          note={note}
          initial={initial}
          onDone={(chosen, text) => done(buildScoreFeedback(runId, candidate, chosen, text))}
        />
      </DialogContent>
    </Dialog>
  )
}

interface ScoreBadgeProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  score: number | null
  name: string
}

// The label fades when the score changes after the row mounted, not on every mount.
const ScoreBadge = forwardRef<HTMLButtonElement, ScoreBadgeProps>(
  ({ score, name, className, ...props }, ref) => {
    const [mountedScore] = useState(score)
    const changed = score !== mountedScore
    return (
      <button
        ref={ref}
        type="button"
        aria-label={`Score ${name}`}
        data-row-action="score"
        className={cn(
          "shrink-0 cursor-pointer whitespace-nowrap rounded-full border border-border bg-secondary px-2 py-1 text-[11px] text-foreground transition-[border-color] duration-fast ease-out hover:border-muted-foreground",
          className,
        )}
        {...props}
      >
        <span
          key={score ?? "none"}
          className={cn("inline-block", changed && "animate-[rise-in_var(--t-med)_var(--ease-out)]")}
        >
          {score === null ? "Score" : `Your score: ${score}/${SCORE_SCALE}`}
        </span>
      </button>
    )
  },
)
ScoreBadge.displayName = "ScoreBadge"

interface ScoreFormProps {
  choices: RubricChoice[]
  score: number | null
  note: string
  initial: RefObject<HTMLInputElement>
  onDone: (score: number, note: string) => void
}

function ScoreForm({ choices, score: saved, note: savedNote, initial, onDone }: ScoreFormProps) {
  const [score, setScore] = useState(saved)
  const [note, setNote] = useState(savedNote)
  const [hovered, setHovered] = useState<number | null>(null)
  const active = useMemo(
    () => choices.find((choice) => choice.score === (hovered ?? score)),
    [choices, hovered, score],
  )
  const focusScore = score ?? choices[0]?.score

  function save() {
    if (score !== null) onDone(score, note)
  }

  // A choice made by key or click shows its meaning even while the pointer rests on another.
  function choose(value: number) {
    setScore(value)
    setHovered(null)
  }

  // On a choice: 1–5 choose, Enter saves. In the notes, ⌘/Ctrl + Enter saves.
  function onChoiceKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault()
      save()
      return
    }
    const choice = choices.find((option) => String(option.score) === event.key)
    if (!choice) return
    event.preventDefault()
    choose(choice.score)
    event.currentTarget.form?.querySelector<HTMLInputElement>(`input[value="${choice.score}"]`)?.focus()
  }

  return (
    <form
      className="grid gap-5"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <fieldset className="m-0 grid gap-3 border-0 p-0">
        <legend className="mb-3 p-0 text-sm font-medium">Your score</legend>
        {/* Left as a whole, so crossing the gap between two choices keeps the line steady. */}
        <div className="grid grid-cols-5 gap-2" onPointerLeave={() => setHovered(null)}>
          {choices.map((choice) => (
            <label
              key={choice.score}
              className="relative cursor-pointer"
              onPointerEnter={() => setHovered(choice.score)}
            >
              <input
                ref={choice.score === focusScore ? initial : undefined}
                type="radio"
                name="score"
                value={choice.score}
                checked={choice.score === score}
                onChange={() => choose(choice.score)}
                onKeyDown={onChoiceKey}
                aria-label={`Score ${choice.score}: ${choice.meaning}`}
                className="peer absolute size-px opacity-0"
              />
              <span className="grid h-11 place-items-center rounded-[6px] border border-border bg-background text-sm font-semibold tabular-nums transition-[background-color,border-color,color,transform] duration-fast ease-out hover:bg-secondary active:scale-95 peer-checked:border-primary peer-checked:bg-primary peer-checked:text-white peer-focus-visible:outline peer-focus-visible:outline-[3px] peer-focus-visible:outline-offset-2 peer-focus-visible:outline-muted-foreground">
                {choice.score}
              </span>
            </label>
          ))}
        </div>
        <p
          key={active?.score ?? "none"}
          aria-hidden
          className="m-0 min-h-[2.8em] animate-[fade-in_var(--t-fast)_var(--ease-out)] text-[13px] leading-snug text-muted-foreground"
        >
          {active ? (
            <>
              <b className="font-semibold text-foreground">{active.name}</b>
              {active.detail ? ` — ${active.detail}` : null}
            </>
          ) : (
            "Choose a score from 1 to 5."
          )}
        </p>
      </fieldset>
      <NotesField
        label="Notes"
        optional
        placeholder="Why this score?"
        value={note}
        onChange={setNote}
        onSave={save}
      />
      <FeedbackFooter action="Save" disabled={score === null} />
    </form>
  )
}
