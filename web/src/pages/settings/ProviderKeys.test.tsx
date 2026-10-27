import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { ProviderSettings } from "./ProviderSettings"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const row = (masked: string | null) => ({
  providers: [
    {
      id: "openai",
      name: "OpenAI",
      description: "Models and embeddings",
      connected: masked !== null,
      credentials: [{ name: "OPENAI_API_KEY", configured: masked !== null, masked }],
      connect_url: "https://platform.openai.com/api-keys",
    },
  ],
})

it("a key typed into the dialog is posted and the row shows it", async () => {
  const fetch = vi
    .fn<(input: string, init?: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(new Response(JSON.stringify(row(null))))
    .mockResolvedValueOnce(new Response(JSON.stringify(row("••••9876"))))
  vi.stubGlobal("fetch", fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ProviderSettings />
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole("button", { name: "Enter key" }))
  const field = await screen.findByLabelText("OPENAI_API_KEY")
  expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true)
  fireEvent.change(field, { target: { value: "sk-synthetic-9876" } })
  fireEvent.click(screen.getByRole("button", { name: "Save" }))
  await waitFor(() => expect(screen.getByText("••••9876")).toBeTruthy())
  const [url, init] = fetch.mock.calls[1] ?? []
  expect(url).toBe("/api/settings/providers")
  expect(init?.method).toBe("POST")
  expect(JSON.parse(typeof init?.body === "string" ? init.body : "null")).toEqual({
    values: { OPENAI_API_KEY: "sk-synthetic-9876" },
  })
  expect(screen.getByRole("button", { name: "Change key" })).toBeTruthy()
})
