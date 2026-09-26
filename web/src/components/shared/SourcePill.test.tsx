import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SourcePill } from "./SourcePill";
import { SourcePills } from "./SourcePills";

afterEach(cleanup);

describe("SourcePill", () => {
  it.each([
    ["gmail", "text-[#4ade80]", "Gmail"],
    ["imessage", "text-[#34d399]", "iMessage"],
    ["whatsapp", "text-[#34d399]", "WhatsApp"],
    ["linkedin", "text-[#60a5fa]", "LinkedIn"],
  ] as const)("draws %s in its colour with a titled icon", (channel, colour, title) => {
    const { container } = render(<SourcePill channel={channel} />);
    const pill = container.querySelector(`[data-c="${channel}"]`);
    expect(pill?.className).toContain(colour);
    expect(pill?.getAttribute("title")).toBe(title);
    expect(pill?.querySelector("svg")?.getAttribute("aria-label")).toBe(title);
  });

  it("fills only the LinkedIn glyph", () => {
    const { container } = render(<SourcePills channels={["gmail", "linkedin"]} />);
    const [gmail, linkedin] = container.querySelectorAll("svg");
    expect(gmail?.getAttribute("fill")).toBe("none");
    expect(linkedin?.getAttribute("fill")).toBe("currentColor");
  });
});
