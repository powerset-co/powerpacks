import { afterEach, describe, expect, it } from "vitest"

import { popoverPlace } from "./place"

interface Box {
  top: number
  right: number
  bottom: number
}

/** A card holding a menu, each answering `getBoundingClientRect` with the given box. */
function cardWithMenu(card: Box, menu: Box, scrollTop = 0): HTMLElement {
  const host = document.createElement("article")
  host.className = "identity-card"
  const anchor = document.createElement("div")
  host.append(anchor)
  document.body.append(host)
  host.getBoundingClientRect = () => new DOMRect(0, card.top, card.right, card.bottom - card.top)
  anchor.getBoundingClientRect = () => new DOMRect(0, menu.top, menu.right, menu.bottom - menu.top)
  host.scrollTop = scrollTop
  return anchor
}

afterEach(() => document.body.replaceChildren())

describe("popoverPlace", () => {
  it("sits 8px under the menu with their right edges in line", () => {
    const menu = cardWithMenu({ top: 200, right: 900, bottom: 700 }, { top: 211, right: 889, bottom: 237 })
    expect(popoverPlace(menu)).toEqual({ top: 45, right: 11 })
  })

  it("keeps 8px from the card's right edge", () => {
    const menu = cardWithMenu({ top: 0, right: 900, bottom: 500 }, { top: 10, right: 898, bottom: 36 })
    expect(popoverPlace(menu)).toEqual({ top: 44, right: 8 })
  })

  it("follows a card scrolled inside itself", () => {
    const menu = cardWithMenu({ top: 0, right: 900, bottom: 500 }, { top: -50, right: 889, bottom: -24 }, 60)
    expect(popoverPlace(menu)).toEqual({ top: 44, right: 11 })
  })

  it("needs the menu to be in a card", () => {
    expect(() => popoverPlace(document.createElement("div"))).toThrow("missing the menu's card")
  })
})
