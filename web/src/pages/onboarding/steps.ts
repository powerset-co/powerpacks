// The onboarding's steps, in order: the window's top bar shows them, each screen is one. The first
// look is the Set up screen's right column once people are in, not a step of its own.

export type StepKey = "signup" | "linkedin" | "setup" | "sources"

export interface Step {
  key: StepKey
  label: string
  href: string
}

const SIGNUP: Step = { key: "signup", label: "Sign up", href: "/onboarding/signup" }

export const STEPS: readonly Step[] = [
  SIGNUP,
  { key: "linkedin", label: "LinkedIn", href: "/onboarding/linkedin" },
  { key: "setup", label: "Set up", href: "/onboarding/setup" },
  { key: "sources", label: "Sources", href: "/onboarding/sources" },
]

export function stepAt(key: string | undefined): Step {
  return STEPS.find((step) => step.key === key) ?? SIGNUP
}

export function after(key: StepKey): Step | undefined {
  return STEPS[STEPS.findIndex((step) => step.key === key) + 1]
}
