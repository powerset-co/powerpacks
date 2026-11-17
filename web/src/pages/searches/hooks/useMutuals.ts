import { useState } from "react"

import { errorText } from "@/lib/api/http"
import { fetchMutuals, type MutualsAnswer } from "@/lib/api/linkedin"

export interface Mutuals {
  state: "idle" | "loading" | MutualsAnswer | Error
  get: () => void
}

// The drawer's Get Mutuals for one person: nothing is asked until `get`. The answer is kept with
// the profile it is for, so the next person opened starts idle.
export function useMutuals(linkedinUrl: string): Mutuals {
  const [held, setHeld] = useState<{ url: string; state: Mutuals["state"] }>({ url: "", state: "idle" })
  const get = () => {
    setHeld({ url: linkedinUrl, state: "loading" })
    fetchMutuals(linkedinUrl).then(
      (answer) => setHeld({ url: linkedinUrl, state: answer }),
      (error: unknown) => setHeld({ url: linkedinUrl, state: new Error(errorText(error)) }),
    )
  }
  return { state: held.url === linkedinUrl ? held.state : "idle", get }
}
