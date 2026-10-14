import { describe, expect, it } from "vitest"

import type { Entry } from "@/types/agent"

import { chatRun } from "./searchRun"

function command(id: string, text: string): Entry {
  return { kind: "command", id, command: text, status: "done", exitCode: 0, output: "", durationMs: 1 }
}

describe("chatRun", () => {
  it("picks the newest listed run the chat's commands named", () => {
    const entries = [
      command("a", "uv run python search_network_pipeline.py prepare --output-dir .powerpacks/search/swe-sf"),
      { kind: "user" as const, id: "u", text: "again" },
      command("b", "uv run python deep_search_loop.py --run-dir .powerpacks/deep-search/staff-eng-jd"),
      command("c", "cat .powerpacks/search/not-listed/decision.json"),
    ]
    expect(chatRun(entries, new Set(["swe-sf", "staff-eng-jd"]))).toBe("staff-eng-jd")
  })

  it("is null before a search is saved", () => {
    expect(chatRun([command("a", "cat packs/search/skills/search/SKILL.md")], new Set(["x"]))).toBeNull()
  })
})
