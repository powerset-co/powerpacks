import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ComposerToggle } from "./ComposerToggle"
import { invoke } from "@/lib/desktop"
import { errorText } from "@/lib/api/http"

const KEY = ["codex", "auto-reply"]

export function AutoReply() {
  const client = useQueryClient()
  const preference = useQuery({ queryKey: KEY, queryFn: () => invoke("codex_auto_reply"), retry: false })
  const save = useMutation({
    mutationFn: (enabled: boolean) => invoke("codex_auto_reply", { enabled }),
    onSuccess: (enabled) => client.setQueryData(KEY, enabled),
  })
  const enabled = preference.data === true
  const error = save.error ?? preference.error
  return (
    <span className="relative inline-flex">
      <ComposerToggle
        label="Auto Reply"
        enabled={enabled}
        disabled={preference.isPending || !!preference.error || save.isPending}
        onChange={() => save.mutate(!enabled)}
        description="Automatically answers incoming @mentions across all chats and searches while the app is open. Uses your ChatGPT allowance; replies are shared with the set."
      />
      {error && (
        <span
          role="alert"
          className="absolute bottom-full mb-2 w-64 rounded-md border border-line bg-card p-2 text-xs text-bad"
        >
          {errorText(error)}
        </span>
      )}
    </span>
  )
}
