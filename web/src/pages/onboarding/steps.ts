// The onboarding's steps, in order: the window's top bar shows them, each screen is one. ChatGPT is
// the Codex sign-in the chat runs on. The first
// look is the Set up screen's right column once people are in, not a step of its own.

export type StepKey = "signup" | "codex" | "linkedin" | "setup"

export interface Step {
  key: StepKey
  label: string
  href: string
}

const SIGNUP: Step = { key: "signup", label: "Sign up", href: "/onboarding/signup" }

export const STEPS: readonly Step[] = [
  SIGNUP,
  { key: "codex", label: "ChatGPT", href: "/onboarding/codex" },
  { key: "linkedin", label: "LinkedIn", href: "/onboarding/linkedin" },
  { key: "setup", label: "Set up", href: "/onboarding/setup" },
]

export function stepAt(key: string | undefined): Step {
  return STEPS.find((step) => step.key === key) ?? SIGNUP
}

/** The screen after this one; initial onboarding ends at Set up. */
export function after(key: StepKey): Step | undefined {
  const steps = STEPS
  return steps[steps.findIndex((step) => step.key === key) + 1]
}
