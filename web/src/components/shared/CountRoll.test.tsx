import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { CountRoll } from "./CountRoll";

// motion reads prefers-reduced-motion once and then follows its change event.
let reduce = true;
const listeners: Array<() => void> = [];
function setReducedMotion(next: boolean) {
  reduce = next;
  listeners.forEach((listener) => listener());
}

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return reduce;
    },
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: (_type: string, listener: () => void) => listeners.push(listener),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
});

afterEach(cleanup);

describe("CountRoll", () => {
  it("jumps straight to the new value under reduced motion", () => {
    setReducedMotion(true);
    const frame = vi.spyOn(window, "requestAnimationFrame");
    const { rerender } = render(<CountRoll value={12} />);
    rerender(<CountRoll value={1480} />);
    expect(screen.getByText((1480).toLocaleString())).toBeTruthy();
    expect(frame).not.toHaveBeenCalled();
    frame.mockRestore();
  });

  it("rolls through frames otherwise", () => {
    setReducedMotion(false);
    const frame = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    const { rerender } = render(<CountRoll value={0} />);
    rerender(<CountRoll value={100} />);
    expect(frame).toHaveBeenCalled();
    expect(screen.getByText("0")).toBeTruthy();
    frame.mockRestore();
  });
});
