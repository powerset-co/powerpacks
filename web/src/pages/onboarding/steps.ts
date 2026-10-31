// The onboarding's steps, in order: the window's top bar shows them, each screen is one. Chrome asks
// which profile to start from, before any sign-in. ChatGPT is the Codex sign-in the chat runs on. The first
// look is the Set up screen's right column once people are in, not a step of its own.

export type StepKey = "chrome" | "signup" | "codex" | "linkedin" | "setup"

export interface Step {
  key: StepKey
  label: string
  href: string
}

const CHROME: Step = { key: "chrome", label: "Chrome", href: "/onboarding/chrome" }
export const SIGNUP: Step = { key: "signup", label: "Sign up", href: "/onboarding/signup" }

export const STEPS: readonly Step[] = [
  CHROME,
  SIGNUP,
  { key: "codex", label: "ChatGPT", href: "/onboarding/codex" },
  { key: "linkedin", label: "LinkedIn", href: "/onboarding/linkedin" },
  { key: "setup", label: "Set up", href: "/onboarding/setup" },
]

export function stepAt(key: string | undefined): Step {
  return STEPS.find((step) => step.key === key) ?? CHROME
}

/** The screen after this one; initial onboarding ends at Set up. */
export function after(key: StepKey): Step | undefined {
  const steps = STEPS
  return steps[steps.findIndex((step) => step.key === key) + 1]
}
