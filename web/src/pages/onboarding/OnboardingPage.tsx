import { useEffect, type ComponentType } from "react"
import { useParams } from "react-router-dom"

import { FirstLook } from "./FirstLook"
import { LinkedIn } from "./LinkedIn"
import { SetUp } from "./SetUp"
import { SignUp } from "./SignUp"
import { Sources } from "./Sources"
import { stepAt, type StepKey } from "./steps"

const SCREENS: Record<StepKey, ComponentType> = {
  signup: SignUp,
  linkedin: LinkedIn,
  setup: SetUp,
  "first-look": FirstLook,
  sources: Sources,
}

/** The onboarding, one screen per step at /onboarding/<step>: sign up, LinkedIn, set up, first
 *  look, sources. Outside the app shell: it is the whole window, as a first run is. */
export function OnboardingPage() {
  const { step } = useParams()
  const current = stepAt(step)
  useEffect(() => {
    document.title = `${current.label} · Powerpacks`
  }, [current])
  const Screen = SCREENS[current.key]
  return <Screen />
}
