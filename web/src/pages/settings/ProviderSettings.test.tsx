import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { ProviderSettings } from "./ProviderSettings"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("shows masked local keys and connects missing providers without asking vendors", async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        providers: [
          {
            id: "modal",
            name: "Modal",
            description: "Models and embeddings",
            connected: true,
            credentials: [{ name: "OPENAI_API_KEY", configured: true, masked: "••••1234" }],
            connect_url: "https://platform.openai.com/api-keys",
            dashboard_url: "https://platform.openai.com/usage",
          },
          {
            id: "parallel",
            name: "Parallel",
            description: "Web research",
            connected: false,
            credentials: [{ name: "PARALLEL_API_KEY", configured: false, masked: null }],
            connect_url: "https://platform.parallel.ai",
            dashboard_url: "https://platform.parallel.ai",
          },
        ],
      }),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ProviderSettings />
    </QueryClientProvider>,
  )
  expect(await screen.findByText("Connected")).toBeTruthy()
  expect(screen.getByText("••••1234")).toBeTruthy()
  expect(screen.getByText("Not set")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Parallel (optional): Connect" })).toBeTruthy()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/settings/providers")
  expect(screen.getByText("Modal (optional)")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Modal" })).toBeNull()
})
