import { describe, expect, it } from "vitest"

import type { NetworkOperator, PersonAttribution } from "@/types/searches"

import { operatorDetail, sourceFamilies } from "./sources"

function attribution(sources: [string, number][], operators: NetworkOperator[] = []): PersonAttribution {
  return {
    person_id: "p1",
    sources: sources.map(([channel, total_interactions]) => ({
      channel,
      total_interactions,
      operator_count: 1,
    })),
    operators,
    total_interactions: 0,
  }
}

describe("sourceFamilies", () => {
  it("keeps every family the legacy row showed: X, contacts export and phone under Messages", () => {
    const families = sourceFamilies(
      attribution([
        ["linkedin", 0],
        ["twitter", 0],
        ["csv_import", 0],
        ["phone", 3],
        ["imessage", 4],
        ["gmail", 1500],
        ["messages_research", 9],
      ]),
    )
    expect(families.map((family) => [family.channel, family.count])).toEqual([
      ["gmail", "~2k"],
      ["imessage", "7"],
      ["linkedin", ""],
      ["x", ""],
      ["csv_import", ""],
    ])
  })

  it("is empty without attribution", () => {
    expect(sourceFamilies(null)).toEqual([])
  })
})

describe("operatorDetail", () => {
  it("names the operator's sources and their email and message counts", () => {
    expect(
      operatorDetail({
        operator_id: "op-1",
        operator_name: "Drew Kilo",
        channels: ["gmail", "whatsapp", "twitter"],
        gmail_interactions: 1234,
        message_interactions: 5,
        gmail_account_details: [],
      }),
    ).toBe("Gmail · WhatsApp · X · 1,234 emails · 5 messages")
  })
})
