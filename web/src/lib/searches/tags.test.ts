import { beforeEach, describe, expect, it, vi } from "vitest"

import { MemoryStorage } from "@/testing/searches-fixture"
import type { Tagged } from "@/types/searches"

import {
  browserTags,
  existingTag,
  heldTags,
  isPinned,
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

  it("reads the pin in the search's spelling", () => {
    const tagged: Tagged = { tags: ["pinned"], assignments: { a: ["pinned"] } }
    expect(isPinned(tagged, "a")).toBe(true)
    expect(isPinned(tagged, "b")).toBe(false)
    expect(isPinned(NO_TAGS, "a")).toBe(false)
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

describe("browserTags", () => {
  beforeEach(() => vi.stubGlobal("localStorage", new MemoryStorage()))

  it("reads results.js's saved tags, else its old pins as the Pinned tag, else none", () => {
    expect(browserTags("jordan-role")).toEqual(NO_TAGS)
    localStorage.setItem("powerset_pinned_jordan-role", JSON.stringify(["p-casey", 7]))
    expect(browserTags("jordan-role")).toEqual({ tags: ["Pinned"], assignments: { "p-casey": ["Pinned"] } })
    const tagged = { tags: ["Backend"], assignments: { "p-jordan": ["Backend"] } }
    localStorage.setItem("powerset_tagged_jordan-role", JSON.stringify(tagged))
    expect(browserTags("jordan-role")).toEqual(tagged)
  })

  it("ignores what it cannot read", () => {
    localStorage.setItem("powerset_tagged_jordan-role", "{")
    localStorage.setItem("powerset_pinned_jordan-role", JSON.stringify({ not: "a list" }))
    expect(browserTags("jordan-role")).toEqual(NO_TAGS)
  })
})
