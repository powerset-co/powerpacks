import { useEffect, type ComponentType } from "react"
import { Navigate, useParams } from "react-router-dom"

import { Codex } from "./Codex"
import { LinkedIn } from "./LinkedIn"
import { SetUp } from "./SetUp"
import { SignUp } from "./SignUp"
import { stepAt, type StepKey } from "./steps"

const SCREENS: Record<StepKey, ComponentType> = {
  signup: SignUp,
  codex: Codex,
  linkedin: LinkedIn,
  setup: SetUp,
}

/** The onboarding, one screen per step at /onboarding/<step>: sign up, ChatGPT, LinkedIn, set up (with the
 *  first look). Later sources and imports live in Accounts; old bookmarks land there. */
export function OnboardingPage() {
  const { step } = useParams()
  const current = stepAt(step)
  useEffect(() => {
    document.title = `${current.label} · Powerpacks`
  }, [current])
  if (step === "sources" || step === "import")
    return <Navigate to={step === "import" ? "/accounts?view=sync" : "/accounts"} replace />
  const Screen = SCREENS[current.key]
  return <Screen />
}
