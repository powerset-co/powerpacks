import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { EMPTY_AGENT, useAgent, type AgentState } from "@/lib/agent/store"
import { fetchDefaults, fetchModels, type CodexModel } from "@/lib/api/codex"
import { ModelPicker } from "./ModelPicker"

vi.mock("@/lib/api/codex", () => ({ fetchModels: vi.fn(), fetchDefaults: vi.fn() }))
vi.mock("@/lib/agent/store", async (actual: () => Promise<Record<string, unknown>>) => ({
  ...(await actual()),
  useAgent: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const efforts = ["low", "medium", "high"].map((id) => ({ id, description: "" }))
const model = (id: string, isDefault: boolean): CodexModel => ({
  id,
  name: id,
  description: "",
  isDefault,
  defaultEffort: "low",
  efforts,
})
const MODELS = [model("gpt-6.1-terra", true), model("gpt-6.1-sol", false)]

function setup(agent: Partial<AgentState> = {}) {
  vi.mocked(useAgent).mockReturnValue({ ...EMPTY_AGENT, ...agent })
  vi.mocked(fetchModels).mockResolvedValue(MODELS)
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ModelPicker />
    </QueryClientProvider>,
  )
}

const PILL = "Model and effort"

it("with nothing picked, shows the default model and effort a turn runs with, not the model's own low", async () => {
  vi.mocked(fetchDefaults).mockResolvedValue({ model: "gpt-6.1-sol", effort: "high" })
  setup()
  const pill = await screen.findByRole("button", { name: PILL })
  await vi.waitFor(() => expect(pill.textContent).toBe("gpt-6.1-solhigh"))
})

it("keeps Codex's model at the default effort when the user's own Codex names no model, and a pick wins", async () => {
  vi.mocked(fetchDefaults).mockResolvedValue({ model: null, effort: "medium" })
  const first = setup()
  const pill = await screen.findByRole("button", { name: PILL })
  await vi.waitFor(() => expect(pill.textContent).toBe("gpt-6.1-terramedium"))
  first.unmount()
  setup({ model: "gpt-6.1-sol", effort: "low" })
  const picked = await screen.findByRole("button", { name: PILL })
  await vi.waitFor(() => expect(picked.textContent).toBe("gpt-6.1-sollow"))
})
