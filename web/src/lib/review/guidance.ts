// The LinkedIn card's one guidance box, two routes: a LinkedIn profile URL applies directly
// through the FREE decide; any other text is research guidance for the PAID re-research.
// Never the other way round.

export const LINKEDIN_URL_RE = /(?:https?:\/\/)?(?:[a-z]+\.)?linkedin\.com\/in\/[A-Za-z0-9._-]+/i

export type GuidanceRoute =
  /** Free: POST decide with `decision=fix` and `new_url=url`. */
  | { kind: "fix"; url: string }
  /** Paid: POST /retarget with `guidance`. */
  | { kind: "retarget"; guidance: string }
  /** Empty box: nothing is sent. */
  | { kind: "nothing" }

/** Where the guidance box's text goes. Text that contains a profile URL takes the free route
 *  with the URL alone, whatever else was typed around it. */
export function routeGuidance(text: string): GuidanceRoute {
  const guidance = text.trim()
  const url = LINKEDIN_URL_RE.exec(guidance)?.[0]
  if (url) return { kind: "fix", url }
  if (!guidance) return { kind: "nothing" }
  return { kind: "retarget", guidance }
}
