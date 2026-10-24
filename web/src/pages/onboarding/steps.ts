// The onboarding's steps, in order: the window's top bar shows them, each screen is one.

export type StepKey = "signup" | "linkedin" | "setup" | "first-look" | "sources"

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
  { key: "first-look", label: "First look", href: "/onboarding/first-look" },
  { key: "sources", label: "Sources", href: "/onboarding/sources" },
]

export function stepAt(key: string | undefined): Step {
  return STEPS.find((step) => step.key === key) ?? SIGNUP
}

export function after(key: StepKey): Step | undefined {
  return STEPS[STEPS.findIndex((step) => step.key === key) + 1]
}
