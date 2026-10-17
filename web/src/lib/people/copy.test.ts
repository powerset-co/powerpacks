import { describe, expect, it } from "vitest"

import { eventDate, label, sentence, titleCase } from "./copy"

describe("copy", () => {
  it("keeps brand casing", () => {
    expect(sentence("imessage")).toBe("iMessage")
    expect(sentence("close_friend")).toBe("Close friend")
    expect(sentence(163348)).toBe("163348")
    expect(titleCase("vice-president")).toBe("Vice-President")
    expect(titleCase("software_engineer")).toBe("Software Engineer")
    expect(titleCase("imessage group")).toBe("iMessage Group")
    expect(titleCase(undefined)).toBe("")
    expect(sentence(undefined)).toBe("")
    expect(sentence("")).toBe("")
  })

  it("maps machine values to words, else sentence case", () => {
    expect(label("reason", "human_worth")).toBe("Your call")
    expect(label("reason", "service_provider")).toBe("Service provider")
  })

  it("writes fact dates in words", () => {
    expect(eventDate("2012-02 to 2012-03")).toBe("Feb 2012 to Mar 2012")
    expect(eventDate("2020-06-04")).toBe("Jun 4, 2020")
    expect(eventDate("2019")).toBe("2019")
  })
})
