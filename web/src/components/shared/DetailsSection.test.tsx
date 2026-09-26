import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DetailsSection } from "./DetailsSection";

afterEach(cleanup);

function Harness({ onToggle }: { onToggle: (open: boolean) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <DetailsSection
      sectionKey="facts"
      title="Facts"
      count={3}
      open={open}
      onToggle={(next) => {
        onToggle(next);
        setOpen(next);
      }}
    >
      <p>Jordan Bravo works at Example Co.</p>
    </DetailsSection>
  );
}

describe("DetailsSection", () => {
  it("reports the new open state and follows it", () => {
    const onToggle = vi.fn();
    const { container } = render(<Harness onToggle={onToggle} />);
    const details = container.querySelector("details[data-section='facts']") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(screen.getByText("3").tagName).toBe("SMALL");

    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(onToggle).toHaveBeenLastCalledWith(true);
    expect(details.open).toBe(true);

    details.open = false;
    fireEvent(details, new Event("toggle"));
    expect(onToggle).toHaveBeenLastCalledWith(false);
    expect(details.open).toBe(false);
    expect(onToggle).toHaveBeenCalledTimes(2);
  });
});
