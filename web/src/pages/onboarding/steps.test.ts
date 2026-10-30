import { expect, it } from "vitest"

import { after, STEPS } from "./steps"

it("the bar is Chrome, Sign up, ChatGPT, LinkedIn, Set up, and Set up is the last", () => {
  expect(STEPS.map((step) => step.label)).toEqual(["Chrome", "Sign up", "ChatGPT", "LinkedIn", "Set up"])
  expect(after("setup")).toBeUndefined()
})
