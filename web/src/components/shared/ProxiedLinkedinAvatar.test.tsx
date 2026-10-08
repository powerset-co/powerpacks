import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ProxiedLinkedinAvatar } from "./ProxiedLinkedinAvatar"

afterEach(cleanup)

describe("ProxiedLinkedinAvatar", () => {
  it("requests the local proxy and reveals only a loaded image", () => {
    const src = "https://media.licdn.com/dms/image/example?token=expired&v=beta"
    const { container, getByText, rerender } = render(
      <ProxiedLinkedinAvatar name="Jordan Bravo" src={src} size={40} />,
    )
    const img = container.querySelector("img")
    if (!img) throw new Error("Missing avatar image")
    expect(img.getAttribute("src")).toBe(`/api/profile-image?url=${encodeURIComponent(src)}`)
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
