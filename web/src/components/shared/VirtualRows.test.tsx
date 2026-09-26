import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { VirtualRows } from "./VirtualRows";

const ROW = 36;
const VIEWPORT = 360;
const PEOPLE = Array.from({ length: 1000 }, (_, index) => ({ id: `p${index}`, name: `Casey Delta ${index}` }));

// jsdom has no layout; the virtualizer reads the scroll element's offset size.
function stubViewportSize(height: number) {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => height });
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 800 });
}

afterEach(cleanup);

describe("VirtualRows", () => {
  beforeEach(() => stubViewportSize(VIEWPORT));

  it("mounts only the visible window plus overscan inside a full-height spacer", () => {
    const { container } = render(
      <VirtualRows
        items={PEOPLE}
        rowHeight={ROW}
        overscan={5}
        getKey={(person) => person.id}
        renderRow={(person) => <div data-row>{person.name}</div>}
      />,
    );
    const rows = container.querySelectorAll("[data-row]");
    expect(rows.length).toBe(VIEWPORT / ROW + 5);
    expect(rows[0]?.textContent).toBe("Casey Delta 0");
    const spacer = container.firstElementChild?.firstElementChild as HTMLElement;
    expect(spacer.style.height).toBe(`${PEOPLE.length * ROW}px`);
  });

  it("renders nothing while the viewport has no height", () => {
    stubViewportSize(0);
    const { container } = render(
      <VirtualRows items={PEOPLE} rowHeight={ROW} getKey={(person) => person.id} renderRow={(person) => <div data-row>{person.name}</div>} />,
    );
    expect(container.querySelectorAll("[data-row]").length).toBe(0);
  });
});
