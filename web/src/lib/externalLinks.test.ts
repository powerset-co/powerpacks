import { describe, expect, it, vi } from "vitest"

vi.mock("@/lib/desktop", () => ({ isDesktop: () => true, invoke: vi.fn() }))
const openExternal = vi.fn((_url: string) => Promise.resolve())
vi.mock("@/lib/api/install", () => ({ openExternal: (url: string) => openExternal(url) }))

import { outboundLink, routeExternalLinks } from "./externalLinks"

function element(selector: string): Element {
  const found = document.querySelector(selector)
  if (!(found instanceof Element)) throw new Error(`no ${selector}`)
  return found
}

function click(anchor: Element): MouseEvent {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })
  anchor.dispatchEvent(event)
  return event
}

describe("outbound links in the desktop app", () => {
  it("sends a link to another site to the system browser and leaves local links alone", () => {
    const stop = routeExternalLinks()
    document.body.innerHTML =
      '<a id="out" href="https://www.linkedin.com/in/jordan" target="_blank"><svg></svg></a><a id="in" href="/people">People</a>'
    const svg = element("#out svg")
    const event = click(svg)
    expect(event.defaultPrevented).toBe(true)
    expect(openExternal).toHaveBeenCalledWith("https://www.linkedin.com/in/jordan")
    const local = click(element("#in"))
    expect(local.defaultPrevented).toBe(false)
    expect(openExternal).toHaveBeenCalledTimes(1)
    stop()
  })
  it("knows a mailto link and ignores other schemes", () => {
    document.body.innerHTML = '<a id="m" href="mailto:a@b.co">a</a><a id="t" href="tel:1">t</a>'
    const mail = new MouseEvent("click", { bubbles: true })
    Object.defineProperty(mail, "target", { value: element("#m") })
    expect(outboundLink(mail)?.href).toBe("mailto:a@b.co")
    const tel = new MouseEvent("click", { bubbles: true })
    Object.defineProperty(tel, "target", { value: element("#t") })
    expect(outboundLink(tel)).toBeNull()
  })
})
