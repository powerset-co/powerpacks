import { useEffect, type ComponentType } from "react"
import { useParams } from "react-router-dom"

import { Codex } from "./Codex"
import { LinkedIn } from "./LinkedIn"
import { SetUp } from "./SetUp"
import { SignUp } from "./SignUp"
import { Sources } from "./Sources"
import { stepAt, type StepKey } from "./steps"

const SCREENS: Record<StepKey, ComponentType> = {
  signup: SignUp,
  codex: Codex,
  linkedin: LinkedIn,
  setup: SetUp,
  sources: Sources,
  import: SetUp,
}

/** The onboarding, one screen per step at /onboarding/<step>: sign up, ChatGPT, LinkedIn, set up (with the
 *  first look), sources, import (the Set up screen again, over every source). Outside the app shell: it is the whole window, as a first run is. */
export function OnboardingPage() {
  const { step } = useParams()
  const current = stepAt(step)
  useEffect(() => {
    document.title = `${current.label} · Powerpacks`
  }, [current])
  const Screen = SCREENS[current.key]
  return <Screen />
}
