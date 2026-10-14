import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { useFullAccessTip } from "@/lib/agent/fullAccessTip"

import { FullAccessTip } from "./FullAccessTip"

function Tip({ onEnable }: { onEnable: () => void }) {
  const tip = useFullAccessTip()
  return tip.shown ? <FullAccessTip onEnable={onEnable} onDismiss={tip.dismiss} /> : null
}

afterEach(() => {
  cleanup()
  localStorage.clear()
})

it("dismissing hides the tip and keeps it hidden after a reload", () => {
  const onEnable = vi.fn()
  render(<Tip onEnable={onEnable} />)
  expect(screen.getByText(/Tired of approving\?/)).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }))
  expect(screen.queryByRole("complementary", { name: "Tip" })).toBeNull()
  expect(localStorage.getItem("chat.fullAccessTip")).toBe(JSON.stringify("dismissed"))
  cleanup()
  render(<Tip onEnable={onEnable} />)
  expect(screen.queryByRole("complementary", { name: "Tip" })).toBeNull()
})

it("offers the Full access switch", () => {
  const onEnable = vi.fn()
  render(<Tip onEnable={onEnable} />)
  fireEvent.click(screen.getByRole("button", { name: "Turn on Full access" }))
  expect(onEnable).toHaveBeenCalledOnce()
})
