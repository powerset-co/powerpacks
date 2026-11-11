// The refusal the ask routes answer with when they need the sets (share/web/sets.py): `needsAuth`
// when Powerset has no sign-in on this machine.

import { failure } from "@/lib/api/http"
import { isRecord } from "@/lib/utils"

const NEEDS_AUTH = "needs_auth"

/** A refusal: `needsAuth` when Powerset has no sign-in on this machine. */
export class SetsError extends Error {
  readonly needsAuth: boolean
  constructor(message: string, needsAuth: boolean) {
    super(message)
    this.name = "SetsError"
    this.needsAuth = needsAuth
  }
}

/** The refusal a sets route (or an ask route, which asks the sets) answered with. */
export async function refusal(response: Response, fallback: string): Promise<SetsError> {
  let needsAuth = false
  try {
    const payload: unknown = await response.clone().json()
    needsAuth = isRecord(payload) && payload.status === NEEDS_AUTH
  } catch {
    // Not JSON: the text is the message.
  }
  const { message } = await failure(response, fallback)
  return new SetsError(message, needsAuth)
}
