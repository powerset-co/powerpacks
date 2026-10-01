import { errorText } from "@/lib/api/http"
import { postWorth, type WorthRequest } from "@/lib/api/review"
import type { WorthResult } from "@/types/review"

/** How a worth save ended: the server's answer, or its refusal in the server's words. */
export type Saved = { ok: true; result: WorthResult } | { ok: false; message: string }

/** POST /worth as a promise that never rejects: a decision's save runs in the background
 *  while the page moves on, and whoever waits for it reads how it ended. */
export function saveWorth(request: WorthRequest): Promise<Saved> {
  return postWorth(request).then(
    (result): Saved => ({ ok: true, result }),
    (error: unknown): Saved => ({ ok: false, message: errorText(error) }),
  )
}
