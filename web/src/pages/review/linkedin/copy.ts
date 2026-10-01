// The LinkedIn stage's own words: the card and its options, the person menu, the guidance box,
// the feedback popover and the finished panel. Words other stages share live in
// lib/review/copy.ts.

/** "Is this the right profile? Or Skip?": the Skip in the middle is the link. */
export const QUESTION = { usual: "Is this the right profile?", or: " Or ", after: "?" } as const

export const DECISION = {
  no: "No",
  noneOfThese: "None of these",
  use: "Use this profile",
  skip: "Skip",
} as const

export const OPTIONS_INTRO = "We found more than one possible profile — pick the right one."

export const OPTION = {
  researched: "Researched profile — no LinkedIn confirmed",
  unfetched: "LinkedIn — profile not fetched yet",
  researchSummary: "Research summary",
} as const

export function reresearchFailed(note: string): string {
  return `Re-research failed: ${note}`
}

export const GUIDANCE = {
  summary: "Wrong person? Provide LinkedIn or re-research",
  placeholder: "Paste a LinkedIn URL to apply it directly, or describe the right person to re-research",
  submit: "Retarget",
  queued: "Queued — results apply automatically in the background",
} as const

export const MENU = { toggle: "More actions", mark: "⋯", feedback: "Leave feedback" } as const

export const FEEDBACK = {
  placeholder: 'e.g. "Wrong person — this is actually Jane Smith"',
  hint: "↵ ⌘+Enter",
  skip: "Skip",
  send: "Send feedback",
  thanks: "Got it, thanks! 🙏",
  signIn: "Sign in to Powerset",
  waiting: "Waiting for sign-in…",
  signInOpened: "Sign-in opened in your browser — finish there, then Send again.",
} as const

export function feedbackContext(name: string): string {
  return `Feedback on ${name} — wrong or missing info?`
}

export const FINISHED = { title: "LinkedIn Profiles Checked", finish: "Finish" } as const

export function decisionsSaved(count: number): string {
  return `${count} decisions saved`
}

export function researchRunning(count: number): string {
  return `${count} re-research still running`
}
