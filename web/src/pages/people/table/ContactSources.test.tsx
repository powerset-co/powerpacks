import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

import { decodePeople } from "@/lib/api/people"
import { DETAIL, PAYLOAD } from "@/testing/people-fixture"

import { ContactSources } from "./ContactSources"

// jsdom has no matchMedia or Web Animations; the float panel reads both (PeoplePage.test stubLayout).
beforeAll(() => {
  HTMLElement.prototype.animate = vi.fn()
  HTMLElement.prototype.getAnimations = () => []
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: true,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it("shows the emails under the Gmail pill with its count, and the numbers under the message pills", async () => {
  const [first] = decodePeople(PAYLOAD)
  if (!first) throw new Error("fixture has no people")
  const row = { ...first, channels: ["gmail", "imessage"] }
  const detail = {
    ...DETAIL,
    emails: ["jordan@example.com", "jb@work.example"],
    phones: ["+1 555 0100"],
    interactions: { gmail: 524, imessage: 63 },
  }
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(detail)))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ContactSources row={row} />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole("button", { name: `Email and phone for ${row.name}` }))
  const panel = await waitFor(() => screen.getByRole("region", { name: `Email and phone for ${row.name}` }))
  await waitFor(() => expect(within(panel).getByText("jordan@example.com")).toBeTruthy())
  const [email, phone] = panel.querySelectorAll(".contact-kind")
  expect(email?.querySelector("strong")?.textContent).toBe("Email")
  expect(email?.querySelector("[data-c=gmail]")?.textContent).toBe("524")
  expect(email?.textContent).toContain("jb@work.example")
  expect(phone?.querySelector("strong")?.textContent).toBe("Phone")
  expect(phone?.querySelector("[data-c=imessage]")?.textContent).toBe("63")
  expect(phone?.querySelector("[data-c=whatsapp]")).toBeNull()
  expect(phone?.textContent).toContain("+1 555 0100")
})
