import { Page } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { connectRelay } from "@/lib/api/relay"
import { useFeatureSettings, useUpdateFeatures } from "@/lib/features"
import { cn } from "@/lib/utils"

/** What someone turns on for themselves. Sets starts off: turning it on brings the Sets page,
 *  Ask the set, and the relay connection that carries the sets' messages. */
export function SettingsPage() {
  const features = useFeatureSettings()
  const update = useUpdateFeatures()
  const saved = features.data
  // The switch moves on the click; a save that fails puts it back and says why.
  const on = update.isPending && update.variables ? update.variables.sets : saved?.sets === true
  const error = update.error ?? features.error
  return (
    <Page title="Settings">
      <section className="flex items-start gap-4 rounded-[var(--radius-l)] border border-line bg-card p-4">
        <div className="min-w-0 flex-1">
          <h2 id="settings-sets" className="m-0 text-sm font-semibold">
            Sets
          </h2>
          <p className="m-0 mt-1 text-xs leading-relaxed text-muted-foreground">
            Work with teammates in a set: see each other's shared networks and ask about candidates.
          </p>
          {error && (
            <p role="alert" className="m-0 mt-2 text-xs text-bad">
              {errorText(error)}
            </p>
          )}
        </div>
        {/* The switch's room is kept while the setting loads, so nothing moves when it shows. */}
        <div className="h-6 w-10 shrink-0">
          {saved && (
            <button
              type="button"
              role="switch"
              aria-checked={on}
              aria-labelledby="settings-sets"
              disabled={update.isPending}
              onClick={() =>
                update.mutate(
                  { ...saved, sets: !on },
                  // Wakes the relay connection to follow the switch now, not at its next 30 s check.
                  { onSuccess: () => void connectRelay().catch(() => undefined) },
                )
              }
              className={cn(
                "relative block h-6 w-10 cursor-pointer rounded-full border-0 p-0 transition-colors duration-fast ease-out disabled:cursor-default",
                on ? "bg-primary-action" : "bg-line-strong",
              )}
            >
              <span
                className={cn(
                  "absolute top-0.5 left-0.5 size-5 rounded-full bg-white shadow-[var(--shadow-1)] transition-transform duration-fast ease-out",
                  on && "translate-x-4",
                )}
              />
            </button>
          )}
        </div>
      </section>
    </Page>
  )
}
