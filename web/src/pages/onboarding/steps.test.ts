import { expect, it } from "vitest"

import { after, stepAt, STEPS } from "./steps"

it("the bar is Sign up, ChatGPT, LinkedIn, Set up, and Set up is the last", () => {
  expect(STEPS.map((step) => step.label)).toEqual(["Sign up", "ChatGPT", "LinkedIn", "Set up"])
  expect(after("setup")).toBeUndefined()
})

it("Sources and Import still open at their URLs, Sources leading to Import", () => {
  expect(stepAt("sources").href).toBe("/onboarding/sources")
  expect(stepAt("import").href).toBe("/onboarding/import")
  expect(after("sources")?.key).toBe("import")
})
