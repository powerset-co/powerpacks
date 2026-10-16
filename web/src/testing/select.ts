import { fireEvent, screen, within } from "@testing-library/react"

// Radix's Select opens on pointerdown and calls pointer capture and scrollIntoView, which jsdom lacks.
for (const name of ["hasPointerCapture", "releasePointerCapture", "setPointerCapture", "scrollIntoView"]) {
  if (typeof Reflect.get(Element.prototype, name) !== "function") {
    Object.defineProperty(Element.prototype, name, { value: () => false, configurable: true, writable: true })
  }
}

/** Opens the select named `name` (inside `container` when given); its options are then on screen. */
export function open(name: string | RegExp, container?: HTMLElement): HTMLElement {
  const scope = container ? within(container) : screen
  const trigger = scope.getByRole("combobox", { name })
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: "mouse" })
  return trigger
}

/** The open select's options, as their text. */
export function options(): string[] {
  return screen.getAllByRole("option").map((option) => option.textContent || "")
}

/** Opens the select named `name` (inside `container` when given) and picks the option named `option`. */
export function pick(name: string | RegExp, option: string | RegExp, container?: HTMLElement): void {
  open(name, container)
  fireEvent.click(screen.getByRole("option", { name: option }))
}

/** The text a select shows for its pick. */
export function picked(name: string | RegExp, container?: HTMLElement): string {
  const scope = container ? within(container) : screen
  return scope.getByRole("combobox", { name }).textContent || ""
}
