import { describe, expect, it } from "vitest"

import { blocks } from "./markdown"

describe("blocks", () => {
  it("splits paragraphs, lists, code and tables", () => {
    const text = [
      "Found **3** people:",
      "",
      "| Name | Company |",
      "| --- | --- |",
      "| Jordan Bravo | Example Co |",
      "",
      "- one",
      "- two",
      "",
      "```",
      "bin/doctor",
      "```",
    ].join("\n")
    expect(blocks(text)).toEqual([
      { kind: "paragraph", text: "Found **3** people:" },
      { kind: "table", head: ["Name", "Company"], rows: [["Jordan Bravo", "Example Co"]] },
      { kind: "list", ordered: false, items: ["one", "two"] },
      { kind: "code", text: "bin/doctor" },
    ])
  })

  it("reads numbered lists and headings", () => {
    expect(blocks("## Next\n1. Sign in\n2. Import")).toEqual([
      { kind: "heading", text: "Next" },
      { kind: "list", ordered: true, items: ["Sign in", "Import"] },
    ])
  })
})
