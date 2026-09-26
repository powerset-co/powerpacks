import { describe, expect, it } from "vitest";

import { eventDate, label, plural, sentence } from "./copy";

describe("copy", () => {
  it("keeps brand casing", () => {
    expect(sentence("imessage")).toBe("iMessage");
    expect(sentence("close_friend")).toBe("Close friend");
    expect(sentence("")).toBe("");
  });

  it("maps machine values to words, else sentence case", () => {
    expect(label("reason", "human_share")).toBe("You chose to share");
    expect(label("reason", "service_provider")).toBe("Service provider");
  });

  it("pluralizes person as people", () => {
    expect(plural(1, "person")).toBe("1 person");
    expect(plural(2, "person")).toBe("2 people");
  });

  it("writes fact dates in words", () => {
    expect(eventDate("2012-02 to 2012-03")).toBe("Feb 2012 to Mar 2012");
    expect(eventDate("2020-06-04")).toBe("Jun 4, 2020");
    expect(eventDate("2019")).toBe("2019");
  });
});
