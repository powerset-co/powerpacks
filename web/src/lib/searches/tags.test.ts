import { describe, expect, it } from "vitest"

import type { Tagged } from "@/types/searches"

import {
  existingTag,
  heldTags,
  NO_TAGS,
  normalizeTag,
  removeTag,
  TAG_NAME_MAX,
  toggleTag,
  untagPeople,
} from "./tags"

describe("tags", () => {
  it("trims and caps a tag name", () => {
    expect(normalizeTag("  Backend | Infra  ")).toBe("Backend | Infra")
    expect(normalizeTag("x".repeat(50))).toHaveLength(TAG_NAME_MAX)
  })

  it("finds the search's spelling ignoring case", () => {
    expect(existingTag(["Backend"], "backend")).toBe("Backend")
    expect(existingTag(["Backend"], "Infra")).toBeUndefined()
  })

  it("adds a new tag to the search and the person, in the search's spelling", () => {
    const once = toggleTag(NO_TAGS, "a", " Backend ")
    expect(once).toEqual({ tags: ["Backend"], assignments: { a: ["Backend"] } })
    expect(toggleTag(once, "b", "BACKEND")).toEqual({
      tags: ["Backend"],
      assignments: { a: ["Backend"], b: ["Backend"] },
    })
  })

  it("takes a held tag off and drops a person left with none; the tag stays in the search", () => {
    const tagged: Tagged = { tags: ["Backend"], assignments: { a: ["Backend"] } }
    expect(toggleTag(tagged, "a", "backend")).toEqual({ tags: ["Backend"], assignments: {} })
  })

  it("ignores a blank tag", () => {
    expect(toggleTag(NO_TAGS, "a", "   ")).toBe(NO_TAGS)
  })

  it("removes a tag from the search and everyone holding it", () => {
    const tagged: Tagged = {
      tags: ["Backend", "Infra"],
      assignments: { a: ["Backend", "Infra"], b: ["Backend"] },
    }
    expect(removeTag(tagged, "backend")).toEqual({ tags: ["Infra"], assignments: { a: ["Infra"] } })
    expect(removeTag(tagged, "Missing")).toBe(tagged)
  })

  it("untags people and keeps the tag list", () => {
    const tagged: Tagged = { tags: ["Backend"], assignments: { a: ["Backend"], b: ["Backend"] } }
    expect(untagPeople(tagged, ["a"])).toEqual({ tags: ["Backend"], assignments: { b: ["Backend"] } })
  })

  it("lists the tags people hold, first seen first", () => {
    const tagged: Tagged = { tags: ["A", "B"], assignments: { a: ["B"], b: ["A", "B"] } }
    expect(heldTags(tagged, ["a", "b", "c"])).toEqual(["B", "A"])
  })

  it("never mutates its input", () => {
    const tagged: Tagged = { tags: ["Backend"], assignments: { a: ["Backend"] } }
    const before = structuredClone(tagged)
    toggleTag(tagged, "a", "Infra")
    removeTag(tagged, "Backend")
    untagPeople(tagged, ["a"])
    expect(tagged).toEqual(before)
  })
})
