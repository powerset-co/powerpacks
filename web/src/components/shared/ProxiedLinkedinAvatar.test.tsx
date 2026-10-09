import { cleanup, fireEvent, render, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ProxiedLinkedinAvatar } from "./ProxiedLinkedinAvatar"

vi.mock("@/lib/api/thumbnails", () => ({
  thumbnail: vi.fn((src: string) =>
    Promise.resolve(`https://proxy.powerset.dev/profile-image?url=${encodeURIComponent(src)}`),
  ),
}))

afterEach(cleanup)

describe("ProxiedLinkedinAvatar", () => {
  it("requests the local proxy and reveals only a loaded image", async () => {
    const src = "https://media.licdn.com/dms/image/example?token=expired&v=beta"
    const { container, getByText, rerender } = render(
      <ProxiedLinkedinAvatar name="Jordan Bravo" src={src} size={40} />,
    )
    await waitFor(() => expect(container.querySelector("img")).not.toBeNull())
    const img = container.querySelector("img")
    if (!img) throw new Error("Missing avatar image")
    expect(img.getAttribute("src")).toBe(
      `https://proxy.powerset.dev/profile-image?url=${encodeURIComponent(src)}`,
    )
    expect(getByText("JB")).toBeTruthy()
    expect(img.dataset.loaded).toBeUndefined()
    fireEvent.load(img)
    expect(img.dataset.loaded).toBe("true")
    fireEvent.error(img)
    expect(img.dataset.loaded).toBeUndefined()
    rerender(<ProxiedLinkedinAvatar name="Casey Example" src={`${src}2`} size={40} />)
    expect(container.querySelector("img")?.dataset.loaded).toBeUndefined()
    expect(getByText("CE")).toBeTruthy()
  })

  it("preserves non-LinkedIn image sources", () => {
    const src = "https://images.example.com/person.png"
    const { container } = render(<ProxiedLinkedinAvatar name="Jordan Bravo" src={src} size={40} />)
    expect(container.querySelector("img")?.getAttribute("src")).toBe(src)
  })

  it("shows initials without requesting an absent photo", () => {
    const { container, getByText } = render(<ProxiedLinkedinAvatar name="Jordan Bravo" size={26} />)
    expect(container.querySelector("img")).toBeNull()
    expect(getByText("JB")).toBeTruthy()
  })
})
