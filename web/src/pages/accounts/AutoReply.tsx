import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { Button } from "@/components/ui/button"
import { invoke } from "@/lib/desktop"
import { errorText } from "@/lib/api/http"

const KEY = ["codex", "auto-reply"]

export function AutoReply({ connected }: { connected: boolean }) {
  const client = useQueryClient()
  const preference = useQuery({ queryKey: KEY, queryFn: () => invoke("codex_auto_reply"), retry: false })
  const save = useMutation({
    mutationFn: (enabled: boolean) => invoke("codex_auto_reply", { enabled }),
    onSuccess: (enabled) => client.setQueryData(KEY, enabled),
  })
  const enabled = preference.data === true
  const error = save.error ?? preference.error
  return (
    <div className="mt-4 border-t border-line pt-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p id="auto-reply-label" className="m-0 text-[13px] font-medium">
            Auto-reply to @mentions
          </p>
          <p id="auto-reply-help" className="m-0 mt-1 text-xs text-muted-foreground">
            Answers set members using your ChatGPT allowance while the app is open. Replies are shared with
            the set.
          </p>
        </div>
        <Button
          role="switch"
          aria-labelledby="auto-reply-label"
          aria-describedby="auto-reply-help"
          aria-checked={enabled}
          size="sm"
          variant={enabled ? "primary" : "ghost"}
          disabled={preference.isPending || !!preference.error || save.isPending || (!connected && !enabled)}
          onClick={() => save.mutate(!enabled)}
        >
          {enabled ? "On" : "Off"}
        </Button>
      </div>
      {error && (
        <p role="alert" className="mb-0 mt-2 text-xs text-bad">
          {errorText(error)}
        </p>
      )}
    </div>
  )
}
